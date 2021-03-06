#!/usr/bin/env python
# -*- coding: utf-8 -*-
#
# Copyright (C) 2013 Radim Rehurek <me@radimrehurek.com>
# Licensed under the GNU LGPL v2.1 - http://www.gnu.org/licenses/lgpl.html
#
# 
# Code in this file is based on code with made publically available under the
# license given above. Modifications were made by Philip Bachman (in 2014).
#

import os
import sys
import heapq
import time
import random
import itertools
try:
    from queue import Queue
except ImportError:
    from Queue import Queue

import numpy as np
import numpy.random as npr
import numba

from six import iteritems, itervalues
from six.moves import xrange

MAX_HSM_KEY = 12345678

class SentenceFileIterator(object):
    """
    Iterator over all files in some directory.

    The directory passed to this object's constructor should contain only text
    files. The text files will be parsed extremely naively, by simply treating
    '\n' characters as delimiters between sentences/phrases/paragraphs, or
    whatever, and then splitting each chunk of text on white space (i.e. by
    applying *.split().
    """
    def __init__(self, dirname):
        self.dirname = dirname
        return

    def __iter__(self):
        for fname in os.listdir(self.dirname):
            if fname.find('.txt') > -1:
                for line in open(os.path.join(self.dirname, fname)):
                    yield line.split()

class Vocab(object):
    """
    A single vocabulary item, used internally when running build_vocab().

    This object will end up holding pointers to the LUT key, HSM code params,
    and various other useful things for each word in a built vocabulary.
    """
    def __init__(self, **kwargs):
        self.count = 0
        self.__dict__.update(kwargs)
        return

    def __lt__(self, other):  # used for sorting in a priority queue
        return self.count < other.count

    def __str__(self):
        vals = ['%s:%r' % (key, self.__dict__[key]) for key in sorted(self.__dict__) if not key.startswith('_')]
        return "<" + ', '.join(vals) + ">"

def build_vocab(sentences, min_count=5, compute_hs_tree=True, \
                compute_ns_table=True, down_sample=0.0):
    """
    Build vocabulary from a sequence of sentences (can be a once-only generator stream).
    Each sentence must be an iterable sequence of hashable objects.
    """
    # scan the given corpus of sentences and count the occurrences of each word
    sentence_no = -1
    raw_vocab = {}
    total_words = 0
    for sentence_no, sentence in enumerate(sentences):
        if sentence_no % 10000 == 0:
            print("PROGRESS: at sentence #%i, processed %i words and %i word types" % \
                (sentence_no, total_words, len(raw_vocab)))
        for word in sentence:
            total_words += 1
            if word in raw_vocab:
                raw_vocab[word].count += 1
            else:
                raw_vocab[word] = Vocab(count=1)
    print("collected %i word types from a corpus of %i words and %i sentences" % \
        (len(raw_vocab), total_words, sentence_no + 1))

    # assign a unique index to each sufficiently frequent word
    #
    # NOTE: If *UNK* is already present in the source files, we will carry it
    # over into the training vocabulary whether or not it meets the frequency
    # threshold. All other unique tokens/words that don't meet the frequency
    # threshold will be treated as if "converted" to *UNK*. The total frequency
    # for *UNK* will thus be the frequency of the "raw" token *UNK* in the
    # source text plus the summed frequencies of all words in the source text
    # that do not meet the frequency threshold on their own. If *UNK* was not
    # present in the source text as a raw token, it will be added to the vocab
    # and will collect the frequencies of all dropped words.
    words_to_vocabs, words_to_keys, keys_to_words = {}, {}, {}
    idx = 0
    unk_count = 0
    for word, v in iteritems(raw_vocab):
        if ((v.count >= min_count) or (word == '*UNK*')):
            # this word meets the frequency threshold or is *UNK*
            v.index = idx
            words_to_vocabs[word] = v
            words_to_keys[word] = idx
            keys_to_words[idx] = word
            idx += 1
        else:
            # collect count for a word that will become *UNK*
            unk_count += v.count
    if '*UNK*' in raw_vocab:
        # *UNK* must have been processed in the above loop
        words_to_vocabs['*UNK*'].count += unk_count
    else:
        # *UNK* was not processed by the above loop, so add it now
        words_to_vocabs['*UNK*'] = Vocab(count=unk_count, index=idx)
        words_to_keys['*UNK*'] = idx
        keys_to_words[idx] = '*UNK*'
    print("total %i word types after removing those with count<%s" % \
        (len(words_to_vocabs), min_count))

    # precalculate downsampling thresholds, which are written into the vocab
    # objects in words_to_vocabs
    _precalc_downsampling(words_to_vocabs, down_sample=down_sample)

    #
    # The objects returned after building the vocabulary are as follows:
    #
    #   words_to_vocabs: map from textish words to their Vocab objects, which
    #                    we keep because the Vocab object contains the word's
    #                    count (i.e. # appearances in corpus) and its computed
    #                    downsampling frequency for use in training.
    #
    #   words_to_keys: map from textish words to the LUT keys that we will use
    #                  to fetch their various embedding vectors/parameters
    #
    #   keys_to_words: map from LUT keys to the textish words that they will
    #                  stand in for during model training
    #
    #   unk_word: the textish representation that stands in for words that
    #             aren't included in the "trained" vocabulary
    #
    #   ns_table: a large list of word LUT keys such that sampling keys
    #             uniformly at random from the list samples the keys nearly in
    #             proportion to the frequency of their corresponding words
    #
    #   hs_tree: dict containing containing three items: 'keys_to_code_keys',
    #            'keys_to_code_signs', and 'max_code_key'.
    #     keys_to_code_keys: this maps word LUT keys to their corresponding
    #                        sequence of keys into a LUT containing HSM code
    #                        vectors. 
    #     keys_to_code_signs: like keys_to_code_keys, but maps to the target
    #                         predictions (i.e. +/- 1) for each HSM code.
    #     max_code_key: the maximum key required by the HSM codes recorded in
    #                   keys_to_code_keys.
    #
    #     NOTE: All HSM code keys/signs are stored in a key/sign matrix, so all
    #           codes have the same length, in some sense. However, to be most
    #           efficient, HSM codes should be variable length. So, each row in
    #           the key/sign matrix is not entirely filled with valid values.
    #           Unused entries in the key matrix are set to > MAX_HSM_KEY, and
    #           unused entries in the sign matrix are set to 0. This lets us
    #           use the "fixed-length" codes just like variable-length codes.
    #                            
    result = {}
    result['words_to_vocabs'] = words_to_vocabs
    result['words_to_keys'] = words_to_keys
    result['keys_to_words'] = keys_to_words
    result['unk_word'] = '*UNK*'
    result['hs_tree'] = None
    result['ns_table'] = None
    if compute_hs_tree:
        result['hs_tree'] = _create_binary_tree(words_to_vocabs)
    if compute_ns_table:
        # build the table for drawing random words (for negative sampling)
        result['ns_table'] = _make_table(words_to_vocabs, keys_to_words, \
                words_to_keys)
    return result

def _precalc_downsampling(w2v, down_sample=0.0):
    """
    Precalculate each vocabulary item's retention probability.

    Called from `build_vocab()`.
    """
    assert(down_sample >= 0.0)
    sample = (down_sample > 1e-8)
    total_words = sum([v.count for v in itervalues(w2v)])
    for v in itervalues(w2v):
        prob = 1.0
        if sample:
            prob = np.sqrt(down_sample / (v.count / total_words))
        v.sample_prob = min(prob, 1.0)
    return

def _make_table(w2v, k2w, w2k, table_size=20000000, power=0.75):
    """
    Create a table using stored vocabulary word counts for drawing random words
    in parts of training based on 'negative sampling'.

    Called from `build_vocab()`.
    """
    # table (= list of words) of noise distribution for negative sampling
    vocab_size = len(k2w)
    table = np.zeros((table_size,), dtype=np.uint32)

    # compute sum of all power (Z in paper)
    power_sum = float(sum([w2v[word].count**power for word in w2v]))
    # go through the whole table and fill it up with the word indexes proportional to a word's count**power
    widx = 0
    # normalize count^0.75 by Z
    d1 = w2v[k2w[widx]].count**power / power_sum
    for tidx in xrange(table_size):
        table[tidx] = widx
        if ((float(tidx) / table_size) > d1):
            widx += 1
            d1 += w2v[k2w[widx]].count**power / power_sum
        if widx >= vocab_size:
            widx = vocab_size - 1
    return table.astype(np.uint32)


def _create_binary_tree(w2v):
    """
    Create a binary Huffman tree using stored vocabulary word counts. Frequent words
    will have shorter binary codes. Called internally from `build_vocab()`.

    The codes (presumably for use in a Hierarchical Softmax Layer) are stored
    directly into the Vocab ojects that are the values in the dict param 'w2v'.
    """
    # build the huffman tree
    heap = list(itervalues(w2v))
    heapq.heapify(heap)
    for i in xrange(len(w2v) - 1):
        min1, min2 = heapq.heappop(heap), heapq.heappop(heap)
        heapq.heappush(heap, Vocab(count=(min1.count + min2.count), \
                index=(i + len(w2v)), left=min1, right=min2))
    # recurse over the tree, assigning a binary code to each vocabulary word
    key_dict = {} # map from word LUT keys to HSM code keys
    sign_dict = {} # map from word LUT keys to HSM code signs
    if heap:
        stack = [(heap[0], [], [])]
        while stack:
            node, signs, keys = stack.pop()
            if node.index < len(w2v):
                # leaf node => store its path from the root
                key_dict[node.index] = keys
                sign_dict[node.index] = signs
            else:
                # inner node => continue recursion
                keys = np.array(list(keys) + [node.index - len(w2v)], dtype=np.uint32)
                stack.append((node.left, np.array(list(signs) + [-1.0], dtype=np.float32), keys))
                stack.append((node.right, np.array(list(signs) + [1.0], dtype=np.float32), keys))
    # get the max code length and maximum word LUT key
    max_code_len = np.max(np.array([v.size for v in key_dict.values()]))
    max_word_key = np.max(np.array([k for k in key_dict.keys()]))
    # extend all v.codes/v.points to the same size
    max_code_key =0
    code_keys = np.zeros((max_word_key+1, max_code_len), dtype=np.uint32)
    code_signs = np.zeros((max_word_key+1, max_code_len), dtype=np.float32)
    for k in key_dict.keys():
        c_len = key_dict[k].size
        for j in range(max_code_len):
            if (j >= c_len):
                code_keys[k,j] = MAX_HSM_KEY + 1
            else:
                code_keys[k,j] = key_dict[k][j]
                code_signs[k,j] = sign_dict[k][j]
                if (code_keys[k,j] > max_code_key):
                    max_code_key = code_keys[k,j]
    # record hsm code keys and signs for returnage
    hsm_tree = {}
    hsm_tree['keys_to_code_keys'] = code_keys.astype(np.uint32)
    hsm_tree['keys_to_code_signs'] = code_signs.astype(np.float32)
    hsm_tree['max_code_key'] = max_code_key
    return hsm_tree

def sample_phrases(text_stream, words_to_keys, unk_word='*UNK*', \
                    max_phrases=100000):
    phrases = []
    for text_blob in text_stream:
        p_keys = np.zeros((len(text_blob),), dtype=np.uint32)
        for (i, word) in enumerate(text_blob):
            if word in words_to_keys:
                p_keys[i] = words_to_keys[word]
            else:
                p_keys[i] = words_to_keys[unk_word]
        phrases.append(p_keys.astype(np.uint32))
        if len(phrases) >= max_phrases:
            break
    return phrases

###################################
# TRAINING EXAMPLE SAMPLING UTILS #
###################################

@numba.jit("void(u4[:], i8, i8, i8, u4[:], u4[:], u4[:], u4[:])")
def fast_pair_sample(phrase, max_window, i, repeats, anc_keys, pos_keys, rand_pool, ri):
    phrase_len = phrase.size
    for r in range(repeats):
        j = i + r
        a_idx = rand_pool[ri[0]] % phrase_len
        ri[0] += 1
        red_win = (rand_pool[ri[0]] % max_window) + 1
        ri[0] += 1
        c_min = a_idx - red_win
        if (c_min < 0):
            c_min = 0
        c_max = a_idx + red_win
        if (c_max >= phrase_len):
            c_max = phrase_len - 1
        c_span = c_max - c_min + 1
        c_idx = a_idx
        while (c_idx == a_idx):
            c_idx = c_min + (rand_pool[ri[0]] % c_span)
            ri[0] += 1
        anc_keys[j] = phrase[a_idx]
        pos_keys[j] = phrase[c_idx]
    return

@numba.jit("void(u4[:], i8, i8, u4[:], i8, u4[:,:], u4[:], u4[:])")
def fast_seq_sample(phrase, gram_n, pad_key, i, repeats, key_seqs, rand_pool, ri):
    phrase_len = phrase.size
    for r in range(repeats):
        j = i + r
        # Get a random stopping point for the n-gram. For now, assume that
        # n-grams containing fewer than 2 valid words, i.e. a context word and
        # a predicted word, are not desired.
        stop_idx = (rand_pool[ri[0]] % (phrase_len - 1)) + 1
        ri[0] += 1
        # Get the start index of the n-gram (maybe negative)
        start_idx = stop_idx - gram_n + 1
        cur_pos = 0
        while (cur_pos < gram_n):
            # Record the word LUT keys for this n-gram, substituting the
            # "padding key" as required due to phrase length
            if ((start_idx + cur_pos) < 0):
                key_seqs[j,cur_pos] = pad_key[0]
            else:
                key_seqs[j,cur_pos] = phrase[start_idx+cur_pos]
            cur_pos += 1
    return

class PhraseSampler:
    """
    This samples positive example pairs each comprising an anchor word and a
    near-by context word from its "skip-gram window". This can also samples
    n_gram sequences from the managed collection of phrases.
    """
    def __init__(self, phrase_list, max_window, max_phrase_key=50000):
        # phrase_list contains the phrases to sample from
        self.max_window = max_window
        self.phrase_list = phrase_list
        self.phrase_table = self._make_table(self.phrase_list)
        self.max_phrase_key = min(len(self.phrase_list), max_phrase_key)
        self.pt_size = self.phrase_table.size
        return

    def _make_table(self, p_list, table_size=20000000):
        """
        Create a table for quickly drawing phrase indices in proportion to
        the length of each phrase.
        """
        phrase_count = len(p_list)
        phrase_lens = np.asarray([p.size for p in p_list]).astype(np.float64)
        len_sum = np.sum(phrase_lens)
        table = np.zeros((table_size,), dtype=np.uint32)
        widx = 0
        d1 = phrase_lens[0] / len_sum
        for tidx in xrange(table_size):
            table[tidx] = widx
            if ((float(tidx) / table_size) > d1):
                widx += 1
                d1 += phrase_lens[widx] / len_sum
            if widx >= phrase_count:
                widx = phrase_count - 1
        return table.astype(np.uint32)

    def sample_pairs(self, sample_count):
        """Draw a sample."""
        anc_keys = np.zeros((sample_count,), dtype=np.uint32)
        pos_keys = np.zeros((sample_count,), dtype=np.uint32)
        phrase_keys = np.zeros((sample_count,), dtype=np.uint32)
        # we will use a "precomputed" table of random ints, to save overhead
        # on calls through numpy.random. the location of the next fresh random
        # int in rand_pool is given by ri[0]
        rand_pool = npr.randint(0, high=self.pt_size, \
                size=(10*sample_count,)).astype(np.uint32)
        ri = np.asarray([0]).astype(np.uint32) # index into rand_pool
        repeats = 5
        while not ((sample_count % repeats) == 0):
            repeats -= 1
        for i in range(0, sample_count, repeats):
            pt_idx = rand_pool[ri[0]]
            ri[0] = ri[0] + 1
            phrase_keys[i:(i+repeats)] = self.phrase_table[pt_idx]
            fast_pair_sample(self.phrase_list[phrase_keys[i]], self.max_window, \
                             i, repeats, anc_keys, pos_keys, rand_pool, ri)
        anc_keys = anc_keys.astype(np.uint32)
        pos_keys = pos_keys.astype(np.uint32)
        phrase_keys = np.minimum(self.max_phrase_key, phrase_keys).astype(np.uint32)
        return [anc_keys, pos_keys, phrase_keys]

    def sample_ngrams(self, sample_count, gram_n=5, pad_key=None):
        """Draw a sample."""
        key_seqs = np.zeros((sample_count, gram_n), dtype=np.uint32)
        phrase_keys = np.zeros((sample_count,), dtype=np.uint32)
        pad_key = np.asarray([pad_key]).astype(np.uint32)
        # we will use a "precomputed" table of random ints, to save overhead
        # on calls through numpy.random. the location of the next fresh random
        # int in rand_pool is given by ri[0]
        rand_pool = npr.randint(0, high=self.pt_size, \
                size=(10*sample_count,)).astype(np.uint32)
        ri = np.asarray([0]).astype(np.uint32) # index into rand_pool
        repeats = 5
        while not ((sample_count % repeats) == 0):
            repeats -= 1
        for i in range(0, sample_count, repeats):
            pt_idx = rand_pool[ri[0]]
            ri[0] = ri[0] + 1
            phrase_keys[i:(i+repeats)] = self.phrase_table[pt_idx]
            fast_seq_sample(self.phrase_list[phrase_keys[i]], gram_n, pad_key, \
                    i, repeats, key_seqs, rand_pool, ri)
        key_seqs = key_seqs.astype(np.uint32)
        phrase_keys = np.minimum(self.max_phrase_key, phrase_keys).astype(np.uint32)
        return [key_seqs, phrase_keys]

class NegSampler:
    """
    This samples "contrastive words" for training via negative sampling.
    """
    def __init__(self, neg_table=None, neg_count=10):
        # phrase_list contains the phrases to sample from 
        self.neg_table = neg_table
        self.neg_table_size = self.neg_table.size
        self.neg_count = neg_count
        return

    def sample(self, sample_count, neg_count=0):
        if (neg_count == 0):
            neg_count = self.neg_count
        neg_keys = np.zeros((sample_count, neg_count), dtype=np.uint32)
        neg_idx = npr.randint(0, high=self.neg_table_size, size=neg_keys.shape)
        for i in range(neg_keys.shape[1]):
            neg_keys[:,i] = self.neg_table[neg_idx[:,i]]
        neg_keys = neg_keys.astype(np.uint32)
        return neg_keys


if __name__=="__main__":
    sentences = SentenceFileIterator('./training_text')
    result = build_vocab(sentences, min_count=3, down_sample=0.0)




##############
# EYE BUFFER #
##############
