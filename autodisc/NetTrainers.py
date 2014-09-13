import numpy as np
import numpy.random as npr
import os
import sys
import time
from collections import OrderedDict

import theano
import theano.tensor as T
from theano.ifelse import ifelse
import theano.printing

import utils as utils

def shuffle_rows(X_var, Y_var=None):
    """Shuffle a matrix (pair) row-wise, but not in-place on GPU."""
    if Y_var is None:
        np_var = X_var.get_value(borrow=False)
        npr.shuffle(np_var)
        X_var.set_value(np_var)
    else:
        var_1 = X_var.get_value(borrow=False)
        var_2 = Y_var.get_value(borrow=False).reshape((var_1.shape[0],1))
        joint_var = np.hstack((var_1, var_2))
        npr.shuffle(joint_var)
        X_var.set_value(joint_var[:,0:var_1.shape[1]].astype(theano.config.floatX))
        Y_var.set_value(joint_var[:,var_1.shape[0]:].ravel().astype(np.int64))
    return

def train_mlp(
        NET,
        sgd_params,
        datasets):
    initial_learning_rate = sgd_params['start_rate']
    learning_rate_decay = sgd_params['decay_rate']
    n_epochs = sgd_params['epochs']
    batch_size = sgd_params['batch_size']
    wt_norm_bound = sgd_params['wt_norm_bound']
    result_tag = sgd_params['result_tag']
    txt_file_name = "results_mlp_{0}.txt".format(result_tag)
    img_file_name = "weights_mlp_{0}.png".format(result_tag)

    ###########################################################################
    # We will use minibatches for training, as well as for computing stats    #
    # over the validation and testing sets. For Theano reasons, it will be    #
    # easiest if we set up arrays storing the start/end index of each batch   #
    # w.r.t. the relevant observation/class matrices/vectors.                 #
    ###########################################################################
    # Get the training observations and classes
    Xtr, Ytr = (datasets[0][0], T.cast(datasets[0][1], 'int32'))
    Ytr_shared = datasets[0][1]
    tr_samples = Xtr.get_value(borrow=True).shape[0]
    tr_batches = int(np.ceil(tr_samples / float(batch_size)))
    tr_bidx = [[i*batch_size, min(tr_samples, (i+1)*batch_size)] \
            for i in range(tr_batches)]
    tr_bidx = theano.shared(value=np.asarray(tr_bidx, dtype=theano.config.floatX))
    tr_bidx = T.cast(tr_bidx, 'int32')
    # Get the validation and testing observations and classes
    Xva, Yva = (datasets[1][0], T.cast(datasets[1][1], 'int32'))
    Xte, Yte = (datasets[2][0], T.cast(datasets[2][1], 'int32'))
    va_samples = Xva.get_value(borrow=True).shape[0]
    te_samples = Xte.get_value(borrow=True).shape[0]
    va_batches = int(np.ceil(va_samples / 100.))
    te_batches = int(np.ceil(te_samples / 100.))
    va_bidx = [[i*100, min(va_samples, (i+1)*100)] for i in range(va_batches)]
    te_bidx = [[i*100, min(te_samples, (i+1)*100)] for i in range(te_batches)]
    va_bidx = theano.shared(value=np.asarray(va_bidx, dtype=theano.config.floatX))
    te_bidx = theano.shared(value=np.asarray(te_bidx, dtype=theano.config.floatX))
    va_bidx = T.cast(va_bidx, 'int32')
    te_bidx = T.cast(te_bidx, 'int32')

    # Print some useful information about the dataset
    print "dataset info:"
    print "  training samples: {0:d}".format(tr_samples)
    print "  samples/minibatch: {0:d}, minibatches/epoch: {1:d}".format( \
            batch_size, tr_batches)
    print "  validation samples: {0:d}, testing samples: {1:d}".format( \
            va_samples, te_samples)

    ######################
    # build actual model #
    ######################

    print '... building the model'

    # allocate symbolic variables for the data
    index = T.lscalar()  # index to a [mini]batch
    epoch = T.scalar()   # epoch counter
    su_idx = T.lscalar() # symbolic batch index into supervised samples
    un_idx = T.lscalar() # symbolic batch index into unsupervised samples
    x = NET.input        # some observations have labels
    y = T.ivector('y')   # the labels are presented as integer categories
    learning_rate = theano.shared(np.asarray(initial_learning_rate, \
        dtype=theano.config.floatX))

    # Build the expressions for the cost functions. if training without sde or
    # dev regularization, the dev loss/cost will be used, but the weights for
    # dev regularization on each layer will be set to 0 before training.
    train_cost = NET.spawn_class_cost(y) + NET.spawn_reg_cost(y)
    tr_outputs = [train_cost, NET.spawn_class_cost(y), NET.spawn_reg_cost(y)]
    vt_outputs = [NET.proto_class_errors(y), NET.proto_class_loss(y)]

    ############################################################################
    # Compile testing and validation models. these models are evaluated on     #
    # batches of the same size as used in training. trying to jam a large      #
    # validation or test set through the net may take too much memory.         #
    ############################################################################
    test_model = theano.function(inputs=[index], outputs=vt_outputs, \
            givens={ \
                x: Xte[te_bidx[index,0]:te_bidx[index,1],:], \
                y: Yte[te_bidx[index,0]:te_bidx[index,1]]})

    validate_model = theano.function(inputs=[index], outputs=vt_outputs, \
            givens={ \
                x: Xva[va_bidx[index,0]:va_bidx[index,1],:], \
                y: Yva[va_bidx[index,0]:va_bidx[index,1]]})

    ############################################################################
    # prepare momentum and gradient variables, and construct the updates that  #
    # theano will perform on the network parameters.                           #
    ############################################################################
    opt_params = NET.proto_params
    if sgd_params.has_key('top_only'):
        if sgd_params['top_only']:
            opt_params = NET.class_params

    NET_grads = []
    for param in opt_params:
        NET_grads.append(T.grad(train_cost, param))

    NET_moms = []
    for param in opt_params:
        NET_moms.append(theano.shared(np.zeros( \
                param.get_value(borrow=True).shape, dtype=theano.config.floatX)))

    # compute momentum for the current epoch
    mom = ifelse(epoch < 500,
            0.5*(1. - epoch/500.) + 0.99*(epoch/500.),
            0.99)

    # use a "smoothed" learning rate, to ease into optimization
    gentle_rate = ifelse(epoch < 5,
            (epoch / 5.) * learning_rate,
            learning_rate)

    # update the step direction using a momentus update
    dev_updates = OrderedDict()
    for i in range(len(opt_params)):
        dev_updates[NET_moms[i]] = mom * NET_moms[i] + (1. - mom) * NET_grads[i]

    # ... and take a step along that direction
    for i in range(len(opt_params)):
        param = opt_params[i]
        dev_param = param - (gentle_rate * dev_updates[NET_moms[i]])
        # clip the updated param to bound its norm (where applicable)
        if (NET.clip_params.has_key(param) and \
                (NET.clip_params[param] == 1)):
            dev_norms = T.sum(dev_param**2, axis=1, keepdims=1)
            dev_scale = T.clip(T.sqrt(wt_norm_bound / dev_norms), 0., 1.)
            dev_updates[param] = dev_param * dev_scale
        else:
            dev_updates[param] = dev_param

    # compile theano functions for training.  these return the training cost
    # and update the model parameters.
    train_dev = theano.function(inputs=[epoch, index], outputs=tr_outputs, \
            updates=dev_updates, \
            givens={ \
                x: Xtr[tr_bidx[index,0]:tr_bidx[index,1],:], \
                y: Ytr[tr_bidx[index,0]:tr_bidx[index,1]]})

    # theano function to decay the learning rate, this is separate from the
    # training function because we only want to do this once each epoch instead
    # of after each minibatch.
    set_learning_rate = theano.function(inputs=[], outputs=learning_rate, \
            updates={learning_rate: learning_rate * learning_rate_decay})

    ###############
    # train model #
    ###############
    print '... training'

    validation_error = 100.
    test_error = 100.
    min_validation_error = 100.
    min_test_error = 100.
    epoch_counter = 0
    start_time = time.clock()

    results_file = open(txt_file_name, 'wb')
    results_file.write("ensemble description: ")
    results_file.write("  **TODO: Write code for this.**\n")
    results_file.flush()

    e_time = time.clock()
    # get array of epoch metrics (on a single minibatch)
    train_metrics = train_dev(1, 0)
    validation_metrics = validate_model(1)
    test_metrics = test_model(1)
    # compute metrics on testing set
    while epoch_counter < n_epochs:
        ######################################################
        # process some number of minibatches for this epoch. #
        ######################################################
        epoch_counter = epoch_counter + 1
        train_metrics = [0. for v in train_metrics]
        for b_idx in xrange(tr_batches):
            # compute update for some this minibatch
            batch_metrics = train_dev(epoch_counter, b_idx)
            train_metrics = [(em + bm) for (em, bm) in zip(train_metrics, batch_metrics)]
        # Compute 'averaged' values over the minibatches
        train_metrics = [(float(v) / tr_batches) for v in train_metrics]
        # update the learning rate
        new_learning_rate = set_learning_rate()

        ######################################################
        # validation, testing, and general diagnostic stuff. #
        ######################################################
        # compute metrics on validation set
        validation_metrics = [0. for v in validation_metrics]
        for b_idx in xrange(va_batches):
            batch_metrics = validate_model(b_idx)
            validation_metrics = [(em + bm) for (em, bm) in zip(validation_metrics, batch_metrics)]
        # Compute 'averaged' values over the minibatches
        validation_error = 100 * (float(validation_metrics[0]) / va_samples)
        validation_loss = float(validation_metrics[1]) / va_batches

        # compute test error if new best validation error was found
        tag = " "
        # compute metrics on testing set
        test_metrics = [0. for v in test_metrics]
        for b_idx in xrange(te_batches):
            batch_metrics = test_model(b_idx)
            test_metrics = [(em + bm) for (em, bm) in zip(test_metrics, batch_metrics)]
        # Compute 'averaged' values over the minibatches
        test_error = 100 * (float(test_metrics[0]) / te_samples)
        test_loss = float(test_metrics[1]) / te_batches
        if (validation_error < min_validation_error):
            min_validation_error = validation_error
            min_test_error = test_error
            tag = ", test={0:.2f}".format(test_error)
        results_file.write("{0:.2f} {1:.2f} {2:.2f} {3:.4f} {4:.4f} {5:.4f}\n".format( \
                train_metrics[2], validation_error, test_error, train_metrics[1], \
                validation_loss, test_loss))
        results_file.flush()

        # report and save progress.
        print "epoch {0:d}: t_cost={1:.2f}, t_loss={2:.4f}, t_ear={3:.4f}, valid={4:.2f}{5}".format( \
                epoch_counter, train_metrics[0], train_metrics[1], train_metrics[2], \
                validation_error, tag)
        print "--time: {0:.4f}".format((time.clock() - e_time))
        e_time = time.clock()
        # save first layer weights to an image locally
        utils.visualize(NET, 0, 0, img_file_name)

    print("optimization complete. best validation error {0:.4f}, with test error {1:.4f}".format( \
          (min_validation_error), (min_test_error)))

def train_ss_mlp(
        NET,
        sgd_params,
        datasets):
    """
    Train NET using a mix of labeled an unlabeled data.

    Datasets should be a four-tuple, in which the first item is a matrix/vector
    pair of inputs/labels for training, the second item is a matrix of
    unlabeled inputs for training, the third item is a matrix/vector pair of
    inputs/labels for validation, and the fourth is a matrix/vector pair of
    inputs/labels for testing.
    """
    initial_learning_rate = sgd_params['start_rate']
    learning_rate_decay = sgd_params['decay_rate']
    n_epochs = sgd_params['epochs']
    batch_size = sgd_params['batch_size']
    wt_norm_bound = sgd_params['wt_norm_bound']
    result_tag = sgd_params['result_tag']
    txt_file_name = "results_mlp_{0}.txt".format(result_tag)
    img_file_name = "weights_mlp_{0}.png".format(result_tag)

    # Get supervised and unsupervised portions of training data, and create
    # arrays of start/end indices for easy minibatch slicing.
    (Xtr_su, Ytr_su) = (datasets[0][0], T.cast(datasets[0][1], 'int32'))
    (Xtr_un, Ytr_un) = (datasets[1][0], T.cast(datasets[1][1], 'int32'))
    Ytr_su_shared = datasets[0][1]
    Ytr_un_shared = datasets[1][1]
    su_samples = Xtr_su.get_value(borrow=True).shape[0]
    un_samples = Xtr_un.get_value(borrow=True).shape[0]
    tr_batches = 250
    su_bsize = batch_size / 2
    un_bsize = batch_size - su_bsize
    su_batches = int(np.ceil(float(su_samples) / su_bsize))
    un_batches = int(np.ceil(float(un_samples) / un_bsize))
    su_bidx = [[i*su_bsize, min(su_samples, (i+1)*su_bsize)] for i in range(su_batches)]
    un_bidx = [[i*un_bsize, min(un_samples, (i+1)*un_bsize)] for i in range(un_batches)]
    su_bidx = theano.shared(value=np.asarray(su_bidx, dtype=theano.config.floatX))
    un_bidx = theano.shared(value=np.asarray(un_bidx, dtype=theano.config.floatX))
    su_bidx = T.cast(su_bidx, 'int32')
    un_bidx = T.cast(un_bidx, 'int32')
    # get the validation and testing sets and create arrays of start/end
    # indices for easy minibatch slicing
    Xva, Yva = (datasets[2][0], T.cast(datasets[2][1], 'int32'))
    Xte, Yte = (datasets[3][0], T.cast(datasets[3][1], 'int32'))
    va_samples = Xva.get_value(borrow=True).shape[0]
    te_samples = Xte.get_value(borrow=True).shape[0]
    va_batches = int(np.ceil(va_samples / 100.))
    te_batches = int(np.ceil(te_samples / 100.))
    va_bidx = [[i*100, min(va_samples, (i+1)*100)] for i in range(va_batches)]
    te_bidx = [[i*100, min(te_samples, (i+1)*100)] for i in range(te_batches)]
    va_bidx = theano.shared(value=np.asarray(va_bidx, dtype=theano.config.floatX))
    te_bidx = theano.shared(value=np.asarray(te_bidx, dtype=theano.config.floatX))
    va_bidx = T.cast(va_bidx, 'int32')
    te_bidx = T.cast(te_bidx, 'int32')

    # Print some useful information about the dataset
    print "dataset info:"
    print "  supervised samples: {0:d}, unsupervised samples: {1:d}".format( \
            su_samples, un_samples)
    print "  samples/minibatch: {0:d}, minibatches/epoch: {1:d}".format( \
            (su_bsize + un_bsize), tr_batches)
    print "  validation samples: {0:d}, testing samples: {1:d}".format( \
            va_samples, te_samples)

    ######################
    # build actual model #
    ######################

    print '... building the model'

    # allocate symbolic variables for the data
    index = T.lscalar()  # index to a [mini]batch
    epoch = T.scalar()   # epoch counter
    su_idx = T.lscalar() # symbolic batch index into supervised samples
    un_idx = T.lscalar() # symbolic batch index into unsupervised samples
    x = NET.input        # some observations have labels
    y = T.ivector('y')   # the labels are presented as integer categories
    learning_rate = theano.shared(np.asarray(initial_learning_rate, \
        dtype=theano.config.floatX))

    # build the expressions for the cost functions. if training without sde or
    # dev regularization, the dev loss/cost will be used, but the weights for
    # dev regularization on each layer will be set to 0 before training.
    #train_cost = NET.spawn_class_cost(y) + NET.spawn_reg_cost(y)
    class_cost = NET.spawn_class_cost(y)
    if ('ear_type' in sgd_params):
        ear_reg_cost = NET.spawn_reg_cost_alt(y, sgd_params['ear_type'])
    else:
        ear_reg_cost = NET.spawn_reg_cost(y)
    act_reg_cost = NET.act_reg_cost
    if ('ent_lam' in sgd_params):
        train_cost = class_cost + ear_reg_cost + act_reg_cost + \
                NET.spawn_ent_cost(sgd_params['ent_lam'], y)
    else:
        train_cost = class_cost + ear_reg_cost + act_reg_cost
    tr_outputs = [train_cost, class_cost, ear_reg_cost, act_reg_cost]
    vt_outputs = [NET.proto_class_errors(y), NET.proto_class_loss(y)]

    ############################################################################
    # compile testing and validation models. these models are evaluated on     #
    # batches of the same size as used in training. trying to jam a large      #
    # validation or test set through the net may take too much memory.         #
    ############################################################################
    test_model = theano.function(inputs=[index], outputs=vt_outputs, \
            givens={ \
                x: Xte[te_bidx[index,0]:te_bidx[index,1],:], \
                y: Yte[te_bidx[index,0]:te_bidx[index,1]]})

    validate_model = theano.function(inputs=[index], outputs=vt_outputs, \
            givens={ \
                x: Xva[va_bidx[index,0]:va_bidx[index,1],:], \
                y: Yva[va_bidx[index,0]:va_bidx[index,1]]})

    ############################################################################
    # prepare momentum and gradient variables, and construct the updates that  #
    # theano will perform on the network parameters.                           #
    ############################################################################
    opt_params = NET.proto_params
    if sgd_params.has_key('top_only'):
        if sgd_params['top_only']:
            opt_params = NET.class_params

    NET_grads = []
    for param in opt_params:
        NET_grads.append(T.grad(train_cost, param))

    NET_moms = []
    for param in opt_params:
        NET_moms.append(theano.shared(np.zeros( \
                param.get_value(borrow=True).shape, dtype=theano.config.floatX)))

    # compute momentum for the current epoch
    mom = ifelse(epoch < 500,
            0.5*(1. - epoch/500.) + 0.99*(epoch/500.),
            0.99)

    # use a "smoothed" learning rate, to ease into optimization
    gentle_rate = ifelse(epoch < 5,
            ((epoch / 5.) * learning_rate),
            learning_rate)

    # update the step direction using a momentus update
    dev_updates = OrderedDict()
    for i in range(len(opt_params)):
        dev_updates[NET_moms[i]] = mom * NET_moms[i] + (1. - mom) * NET_grads[i]

    # ... and take a step along that direction
    for i in range(len(opt_params)):
        param = opt_params[i]
        dev_param = param - (gentle_rate * dev_updates[NET_moms[i]])
        # clip the updated param to bound its norm (where applicable)
        if (NET.clip_params.has_key(param) and \
                (NET.clip_params[param] == 1)):
            dev_norms = T.sum(dev_param**2, axis=1, keepdims=1)
            dev_scale = T.clip(T.sqrt(wt_norm_bound / dev_norms), 0., 1.)
            dev_updates[param] = dev_param * dev_scale
        else:
            dev_updates[param] = dev_param

    # compile theano functions for training.  these return the training cost
    # and update the model parameters.
    train_dev = theano.function(inputs=[epoch, su_idx, un_idx], outputs=tr_outputs, \
            updates=dev_updates, \
            givens={ \
                x: T.concatenate([Xtr_su[su_bidx[su_idx,0]:su_bidx[su_idx,1],:], \
                        Xtr_un[un_bidx[un_idx,0]:un_bidx[un_idx,1],:]]),
                y: T.concatenate([Ytr_su[su_bidx[su_idx,0]:su_bidx[su_idx,1]], \
                        Ytr_un[un_bidx[un_idx,0]:un_bidx[un_idx,1]]])})

    # theano function to decay the learning rate, this is separate from the
    # training function because we only want to do this once each epoch instead
    # of after each minibatch.
    set_learning_rate = theano.function(inputs=[], outputs=learning_rate, \
            updates={learning_rate: learning_rate * learning_rate_decay})

    ###############
    # train model #
    ###############
    print '... training'

    validation_error = 100.
    test_error = 100.
    min_validation_error = 100.
    min_test_error = 100.
    epoch_counter = 0
    start_time = time.clock()

    results_file = open(txt_file_name, 'wb')
    results_file.write("ensemble description: ")
    results_file.write("  **TODO: Write code for this.**\n")
    results_file.flush()

    e_time = time.clock()
    su_index = 0
    un_index = 0
    # get array of epoch metrics (on a single minibatch)
    train_metrics = train_dev(0, 0, 0)
    validation_metrics = validate_model(0)
    test_metrics = test_model(0)
    # compute metrics on testing set
    while epoch_counter < n_epochs:
        ######################################################
        # process some number of minibatches for this epoch. #
        ######################################################
        epoch_counter = epoch_counter + 1
        train_metrics = [0. for v in train_metrics]
        for b_idx in xrange(tr_batches):
            # compute update for some this minibatch
            batch_metrics = train_dev(epoch_counter, su_index, un_index)
            train_metrics = [(em + bm) for (em, bm) in zip(train_metrics, batch_metrics)]
            su_index = (su_index + 1) if ((su_index + 1) < su_batches) else 0
            un_index = (un_index + 1) if ((un_index + 1) < un_batches) else 0
        # Compute 'averaged' values over the minibatches
        train_metrics = [(float(v) / tr_batches) for v in train_metrics]
        # update the learning rate
        new_learning_rate = set_learning_rate()

        ######################################################
        # validation, testing, and general diagnostic stuff. #
        ######################################################
        # compute metrics on validation set
        validation_metrics = [0. for v in validation_metrics]
        for b_idx in xrange(va_batches):
            batch_metrics = validate_model(b_idx)
            validation_metrics = [(em + bm) for (em, bm) in zip(validation_metrics, batch_metrics)]
        # Compute 'averaged' values over the minibatches
        validation_error = 100 * (float(validation_metrics[0]) / va_samples)
        validation_loss = float(validation_metrics[1]) / va_batches

        # compute test error if new best validation error was found
        tag = " "
        # compute metrics on testing set
        test_metrics = [0. for v in test_metrics]
        for b_idx in xrange(te_batches):
            batch_metrics = test_model(b_idx)
            test_metrics = [(em + bm) for (em, bm) in zip(test_metrics, batch_metrics)]
        # Compute 'averaged' values over the minibatches
        test_error = 100 * (float(test_metrics[0]) / te_samples)
        test_loss = float(test_metrics[1]) / te_batches
        if (validation_error < min_validation_error):
            min_validation_error = validation_error
            min_test_error = test_error
            tag = ", test={0:.2f}".format(test_error)
        results_file.write("{0:.2f} {1:.2f} {2:.2f} {3:.4f} {4:.4f} {5:.4f}\n".format( \
                train_metrics[2], validation_error, test_error, train_metrics[1], \
                validation_loss, test_loss))
        results_file.flush()

        # report and save progress.
        print "epoch {0:d}: t_cost={1:.2f}, t_loss={2:.4f}, t_ear={3:.4f}, t_act={6:.4f}, valid={4:.2f}{5}".format( \
                epoch_counter, train_metrics[0], train_metrics[1], train_metrics[2], \
                validation_error, tag, train_metrics[3])
        print "--time: {0:.4f}".format((time.clock() - e_time))
        e_time = time.clock()
        # save first layer weights to an image locally
        utils.visualize(NET, 0, 0, img_file_name)

    print("optimization complete. best validation error {0:.4f}, with test error {1:.4f}".format( \
          (min_validation_error), (min_test_error)))

def train_dex(
    NET,
    sgd_params,
    datasets):
    """
    Do DEX training.
    """
    initial_learning_rate = sgd_params['start_rate']
    learning_rate_decay = sgd_params['decay_rate']
    n_epochs = sgd_params['epochs']
    batch_size = sgd_params['batch_size']
    wt_norm_bound = sgd_params['wt_norm_bound']
    result_tag = sgd_params['result_tag']
    txt_file_name = "results_dex_{0}.txt".format(result_tag)
    img_file_name = "weights_dex_{0}.png".format(result_tag)

    # Get the training data and create arrays of start/end indices for
    # easy minibatch slicing
    Xtr = datasets[0][0]
    idx_range = np.arange(Xtr.get_value(borrow=True).shape[0])
    Ytr_shared = theano.shared(value=idx_range)
    Ytr = T.cast(Ytr_shared, 'int32')
    tr_samples = Xtr.get_value(borrow=True).shape[0]
    tr_batches = int(np.ceil(float(tr_samples) / batch_size))
    tr_bidx = [[i*batch_size, min(tr_samples, (i+1)*batch_size)] for i in range(tr_batches)]
    tr_bidx = T.cast(tr_bidx, 'int32')

    print "Dataset info:"
    print "  training samples: {0:d}".format(tr_samples)
    print "  samples/minibatch: {0:d}, minibatches/epoch: {1:d}".format( \
        batch_size, tr_batches)

    ######################
    # BUILD ACTUAL MODEL #
    ######################

    print '... building the model'

    # allocate symbolic variables for the data
    index = T.lscalar()  # index to a [mini]batch
    epoch = T.scalar()   # epoch counter
    I = T.ivector()      # keys for the training examples
    x = NET.input        # symbolic matrix for inputs to NET
    learning_rate = theano.shared(np.asarray(initial_learning_rate,
        dtype=theano.config.floatX))

    # Collect the parameters to-be-optimized
    opt_params = NET.proto_params

    # Build the expressions for the cost functions
    NET_dex_cost = NET.spawn_dex_cost(I)
    NET_reg_cost = NET.act_reg_cost
    NET_cost = NET_dex_cost + NET_reg_cost
    NET_metrics = NET_cost

    ############################################################################
    # Prepare momentum and gradient variables, and construct the updates that  #
    # Theano will perform on the network parameters.                           #
    ############################################################################
    NET_grads = []
    for param in opt_params:
        NET_grads.append(T.grad(NET_cost, param))

    NET_moms = []
    for param in opt_params:
        NET_moms.append(theano.shared(np.zeros( \
                param.get_value(borrow=True).shape, dtype=theano.config.floatX)))

    # Compute momentum for the current epoch
    mom = ifelse(epoch < 500,
        0.5*(1. - epoch/500.) + 0.99*(epoch/500.),
        0.99)

    # Use a "smoothed" learning rate, to ease into optimization
    gentle_rate = ifelse(epoch < 10,
        ((epoch / 10.) * learning_rate),
        learning_rate)

    # Update the step direction using a momentus update
    NET_updates = OrderedDict()
    for i in range(len(opt_params)):
        NET_updates[NET_moms[i]] = mom * NET_moms[i] + (1. - mom) * NET_grads[i]

    # ... and take a step along that direction
    for i in range(len(opt_params)):
        param = opt_params[i]
        grad_i = NET_grads[i]
        print("grad_{0:d}.owner.op: {1:s}".format(i, str(grad_i.owner.op)))
        NET_param = param - (gentle_rate * NET_updates[NET_moms[i]])
        # Clip the updated param to bound its norm (where applicable)
        if (NET.clip_params.has_key(param) and \
                (NET.clip_params[param] == 1)):
            NET_norms = T.sum(NET_param**2, axis=1, keepdims=1)
            NET_scale = T.clip(T.sqrt(wt_norm_bound / NET_norms), 0., 1.)
            NET_updates[param] = NET_param * NET_scale
        else:
            NET_updates[param] = NET_param

    # Compile theano functions for training.  These return the training cost
    # and update the model parameters.

    train_NET = theano.function(inputs=[epoch, index], \
        outputs=NET_metrics, \
        updates=NET_updates, \
        givens={ x: Xtr[tr_bidx[index,0]:tr_bidx[index,1],:], \
                 I: Ytr[tr_bidx[index,0]:tr_bidx[index,1]] } ,\
        accept_inplace=True, \
        on_unused_input='warn')

    # Theano function to decay the learning rate, this is separate from the
    # training function because we only want to do this once each epoch instead
    # of after each minibatch.
    set_learning_rate = theano.function(inputs=[], outputs=learning_rate,
        updates={learning_rate: learning_rate * learning_rate_decay})

    ###############
    # TRAIN MODEL #
    ###############
    print '... training'

    epoch_counter = 0
    start_time = time.clock()

    results_file = open(txt_file_name, 'wb')
    results_file.write("ensemble description: ")
    results_file.write("  **TODO: Write code for this.**\n")
    results_file.flush()

    train_metrics = train_NET(0, 0)
    while epoch_counter < n_epochs:
        # Shuffle matrices, to mix up examples to train against eachother
        #shuffle_rows(Xtr, Ytr_shared)

        ######################################################
        # Process some number of minibatches for this epoch. #
        ######################################################
        e_time = time.clock()
        epoch_counter = epoch_counter + 1
        train_cost = 0.0
        for minibatch_index in xrange(tr_batches):
            # Compute update for some joint supervised/unsupervised minibatch
            batch_cost = train_NET(epoch_counter, minibatch_index)
            train_cost += batch_cost
        train_cost = train_cost / tr_batches

        # Update the learning rate
        new_learning_rate = set_learning_rate()

        # Report and save progress.
        print("epoch {0:d}: tr_loss={1:.4f}".format(epoch_counter, train_cost))
        print("--time: {0:.4f}".format((time.clock() - e_time)))
        # Save first layer weights to an image locally
        utils.visualize(NET, 0, 0, img_file_name)















##############
# EYE BUFFER #
##############
