################################################################################
# Code for managing and training a triplet system comprising:                  #
#   1. a generator conditioned on some continuous latent variables             #
#   2. an inferencer for approximating posteriors over the continuous latent   #
#      variables given some input                                              #
#   3. a "classifier" for predicting a posterior over some categorical labels, #
#      which samples from the continuous latent variable posterior as input.   #
################################################################################

# basic python
import numpy as np
import numpy.random as npr
from collections import OrderedDict

# theano business
import theano
import theano.tensor as T
#from theano.tensor.shared_randomstreams import RandomStreams as RandStream
from theano.sandbox.cuda.rng_curand import CURAND_RandomStreams as RandStream

# phil's sweetness
from NetLayers import relu_actfun, softplus_actfun, \
                      safe_softmax, smooth_softmax
from GenNet import GenNet
from InfNet import InfNet
from PeaNet import PeaNet

def log_prob_bernoulli(p_true, p_approx):
    """
    Compute log probability of some binary variables with probabilities
    given by p_true, for probability estimates given by p_approx. We'll
    compute joint log probabilities over row-wise groups.
    """
    log_prob_1 = p_true * T.log(p_approx)
    log_prob_0 = (1.0 - p_true) * T.log(1.0 - p_approx)
    row_log_probs = T.sum((log_prob_1 + log_prob_0), axis=1, keepdims=True)
    return row_log_probs

def log_prob_gaussian(mu_true, mu_approx, le_sigma=1.0):
    """
    Compute log probability of some continuous variables with values given
    by mu_true, w.r.t. gaussian distributions with means given by mu_approx
    and standard deviations given by le_sigma. We assume isotropy.
    """
    ind_log_probs = -( (mu_approx - mu_true)**2.0 / (2.0 * le_sigma**2.0) )
    row_log_probs = T.sum(ind_log_probs, axis=1, keepdims=True)
    return row_log_probs

def cat_entropy(row_dists):
    """
    Compute the entropy of (row-wise) categorical distributions in p.
    """
    row_ents = -T.sum((row_dists * T.log(row_dists)), axis=1, keepdims=True)
    return row_ents

#
#
# Important symbolic variables:
#   Xd: Xd represents input at the "data variables" of the inferencer
#   Yd: Yd represents label information for use in semi-supervised learning
#   Xc: Xc represents input at the "control variables" of the inferencer
#   Xm: Xm represents input at the "mask variables" of the inferencer
#
#

class GIStack1(object):
    """
    Controller for training a variational autoencoder.

    The generator must be an instance of the GenNet class implemented in
    "GenNet.py". The inferencer for the continuous latent variables must be an
    instance of the InfNet class implemented in "InfNet.py". The "classifier"
    for the categorical latent variables must be an instance of the PeaNet
    class implemented in "PeaNet.py".

    Parameters:
        rng: numpy.random.RandomState (for reproducibility)
        Xd: symbolic "data" input to this VAE
        Yd: symbolic "label" input to this VAE
        Xc: symbolic "control" input to this VAE
        Xm: symbolic "mask" input to this VAE
        g_net: The GenNet instance that will serve as the base generator
        i_net: The InfNet instance for inferring continuous posteriors
        p_net: The PeaNet instance for inferring categorical posteriors
        data_dim: dimension of the "observable data" variables
        prior_dim: dimension of the continuous latent variables
        label_dim: cardinality of the categorical latent variable
        batch_size: fixed size of minibatches to be used during training. you
                    have to stick to this value while training. this is to work
                    around theano problems. this only matters for computing the
                    cost function, and doesn't restrict sampling.
        params: dict for passing additional parameters
        shared_param_dicts: dict for retrieving some shared parameters required
                            by a GIPair. if this parameter is passed, then this
                            GIPair will be initialized as a "shared-parameter"
                            clone of some other GIPair.
    """
    def __init__(self, rng=None, \
            Xd=None, Yd=None, Xc=None, Xm=None, \
            g_net=None, i_net=None, p_net=None, \
            data_dim=None, prior_dim=None, label_dim=None, \
            batch_size=None, \
            params=None, shared_param_dicts=None):
        # setup a rng for this GIStack1
        self.rng = RandStream(rng.randint(100000))
        # record the symbolic variables that will provide inputs to the
        # computation graph created to describe this GIStack1
        self.Xd = Xd
        self.Yd = Yd
        self.Xc = Xc
        self.Xm = Xm
        # record the dimensionality of the data handled by this GIStack1
        self.data_dim = data_dim
        self.label_dim = label_dim
        self.prior_dim = prior_dim
        self.batch_size = batch_size
        # create "shared-parameter" clones of the continuous inferencer
        self.IN = i_net.shared_param_clone(rng=rng, \
                Xd=self.Xd, Xc=self.Xc, Xm=self.Xm)
        # capture a handle for the output of the continuous inferencer
        self.Xp = self.IN.output
        # feed it into a shared-parameter clone of the generator
        self.GN = g_net.shared_param_clone(rng=rng, Xp=self.Xp)
        # capture a handle for sampled reconstructions from the generator
        self.Xg = self.GN.output
        # and feed it into a shared-parameter clone of the label inferencer
        self.PN = p_net.shared_param_clone(rng=rng, Xd=self.Xp)
        # capture a handle for the output of the label inferencer. we'll use
        # the output of the "first" spawn-net. it may be useful to try using
        # the output of the proto-net instead...
        self.Yp = safe_softmax(self.PN.output_spawn[0])

        # we will be assuming one proto-net in the pseudo-ensemble represented
        # by self.PN, and either one or two spawn-nets for that proto-net.
        assert(len(self.PN.proto_nets) == 1)
        assert((len(self.PN.spawn_nets) == 1) or \
                (len(self.PN.spawn_nets) == 2))
        # output of the generator and input to the continuous inferencer should
        # both be equal to self.data_dim
        assert(self.data_dim == self.GN.mlp_layers[-1].out_dim)
        assert(self.data_dim == self.IN.shared_layers[0].in_dim)
        # mu/sigma outputs of self.IN should be equal to prior_dim, as should
        # the inputs to self.GN and self.PN. self.PN should produce output with
        # dimension label_dim.
        assert(self.prior_dim == self.IN.mu_layers[-1].out_dim)
        assert(self.prior_dim == self.IN.sigma_layers[-1].out_dim)
        assert(self.prior_dim == self.GN.mlp_layers[0].in_dim)
        assert(self.prior_dim == self.PN.proto_nets[0][0].in_dim)
        assert(self.label_dim == self.PN.proto_nets[0][-1].out_dim)

        # determine whether this GIStack1 is a clone or an original
        if shared_param_dicts is None:
            # This is not a clone, and we will need to make a dict for
            # referring to some important shared parameters.
            self.shared_param_dicts = {}
            self.is_clone = False
        else:
            # This is a clone, and its layer parameters can be found by
            # referring to the given param dict (i.e. shared_param_dicts).
            self.shared_param_dicts = shared_param_dicts
            self.is_clone = True

        if not self.is_clone:
            # shared var learning rate for generator and inferencer
            zero_ary = np.zeros((1,)).astype(theano.config.floatX)
            self.lr_gn = theano.shared(value=zero_ary, name='gis_lr_gn')
            self.lr_in = theano.shared(value=zero_ary, name='gis_lr_in')
            self.lr_pn = theano.shared(value=zero_ary, name='gis_lr_pn')
            # shared var momentum parameters for generator and inferencer
            self.mo_gn = theano.shared(value=zero_ary, name='gis_mo_gn')
            self.mo_in = theano.shared(value=zero_ary, name='gis_mo_in')
            self.mo_pn = theano.shared(value=zero_ary, name='gis_mo_pn')
            # init parameters for controlling learning dynamics
            self.set_all_sgd_params()
            # init shared var for weighting nll of data given posterior sample
            self.lam_nll = theano.shared(value=zero_ary, name='gis_lam_nll')
            self.set_lam_nll(lam_nll=1.0)
            # init shared var for weighting posterior KL-div from prior
            self.lam_kld = theano.shared(value=zero_ary, name='gis_lam_kld')
            self.set_lam_kld(lam_kld=1.0)
            # init shared var for weighting semi-supervised classification
            self.lam_cat = theano.shared(value=zero_ary, name='gis_lam_cat')
            self.set_lam_cat(lam_cat=0.0)
            # init shared var for weighting ensemble agreement regularization
            self.lam_pea = theano.shared(value=zero_ary, name='gis_lam_pea')
            self.set_lam_pea(lam_pea=0.0)
            # init shared var for weighting entropy regularization on the
            # inferred posteriors over the categorical variable of interest
            self.lam_ent = theano.shared(value=zero_ary, name='gis_lam_ent')
            self.set_lam_ent(lam_ent=0.0)
            # init shared var for controlling l2 regularization on params
            self.lam_l2w = theano.shared(value=zero_ary, name='gis_lam_l2w')
            self.set_lam_l2w(lam_l2w=1e-3)
            # record shared parameters that are to be shared among clones
            self.shared_param_dicts['gis_lr_gn'] = self.lr_gn
            self.shared_param_dicts['gis_lr_in'] = self.lr_in
            self.shared_param_dicts['gis_lr_pn'] = self.lr_pn
            self.shared_param_dicts['gis_mo_gn'] = self.mo_gn
            self.shared_param_dicts['gis_mo_in'] = self.mo_in
            self.shared_param_dicts['gis_mo_pn'] = self.mo_pn
            self.shared_param_dicts['gis_lam_nll'] = self.lam_nll
            self.shared_param_dicts['gis_lam_kld'] = self.lam_kld
            self.shared_param_dicts['gis_lam_cat'] = self.lam_cat
            self.shared_param_dicts['gis_lam_pea'] = self.lam_pea
            self.shared_param_dicts['gis_lam_ent'] = self.lam_ent
            self.shared_param_dicts['gis_lam_l2w'] = self.lam_l2w
        else:
            # use some shared parameters that are shared among all clones of
            # some "base" GIStack1
            self.lr_gn = self.shared_param_dicts['gis_lr_gn']
            self.lr_in = self.shared_param_dicts['gis_lr_in']
            self.lr_pn = self.shared_param_dicts['gis_lr_pn']
            self.mo_gn = self.shared_param_dicts['gis_mo_gn']
            self.mo_in = self.shared_param_dicts['gis_mo_in']
            self.mo_pn = self.shared_param_dicts['gis_mo_pn']
            self.lam_nll = self.shared_param_dicts['gis_lam_nll']
            self.lam_kld = self.shared_param_dicts['gis_lam_kld']
            self.lam_cat = self.shared_param_dicts['gis_lam_cat']
            self.lam_pea = self.shared_param_dicts['gis_lam_pea']
            self.lam_ent = self.shared_param_dicts['gis_lam_ent']
            self.lam_l2w = self.shared_param_dicts['gis_lam_l2w']

        # Grab the full set of "optimizable" parameters from the generator
        # and inferencer networks that we'll be working with.
        self.gn_params = [p for p in self.GN.mlp_params]
        self.in_params = [p for p in self.IN.mlp_params]
        self.pn_params = [p for p in self.PN.proto_params]

        ###################################
        # CONSTRUCT THE COSTS TO OPTIMIZE #
        ###################################
        self.data_nll_cost = self.lam_nll[0] * self._construct_data_nll_cost()
        self.post_kld_cost = self.lam_kld[0] * self._construct_post_kld_cost()
        self.post_cat_cost = self.lam_cat[0] * self._construct_post_cat_cost()
        self.post_pea_cost = self.lam_pea[0] * self._construct_post_pea_cost()
        self.post_ent_cost = self.lam_ent[0] * self._construct_post_ent_cost()
        self.other_reg_cost = self._construct_other_reg_cost()
        self.joint_cost = self.data_nll_cost + self.post_kld_cost + self.post_cat_cost + \
                self.post_pea_cost + self.post_ent_cost + self.other_reg_cost

        # Initialize momentums for mini-batch SGD updates. All optimizable
        # parameters need to be safely nestled in their lists by now.
        self.joint_moms = OrderedDict()
        self.gn_moms = OrderedDict()
        self.in_moms = OrderedDict()
        self.pn_moms = OrderedDict()
        for p in self.gn_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape) + 2.0
            self.gn_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.gn_moms[p]
        for p in self.in_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape) + 2.0
            self.in_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.in_moms[p]
        for p in self.pn_params:
            p_mo = np.zeros(p.get_value(borrow=True).shape) + 2.0
            self.pn_moms[p] = theano.shared(value=p_mo.astype(theano.config.floatX))
            self.joint_moms[p] = self.pn_moms[p]

        # now, must construct updates for all parameters and their momentums
        self.joint_updates = OrderedDict()
        self.gn_updates = OrderedDict()
        self.in_updates = OrderedDict()
        self.pn_updates = OrderedDict()
        #######################################
        # Construct updates for the generator #
        #######################################
        for var in self.gn_params:
            # these updates are for trainable params in the generator net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.joint_cost, var, \
                    consider_constant=[self.GN.dist_mean, self.GN.dist_cov])
            # get the momentum for this var
            var_mom = self.gn_moms[var]
            # update the momentum for this var using its grad
            self.gn_updates[var_mom] = (self.mo_gn[0] * var_mom) + \
                    ((1.0 - self.mo_gn[0]) * (var_grad**2.0))
            self.joint_updates[var_mom] = self.gn_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_gn[0] * (var_grad / T.sqrt(var_mom + 1e-1)))
            # apply "norm clipping" if desired
            if ((var in self.GN.clip_params) and \
                    (var in self.GN.clip_norms) and \
                    (self.GN.clip_params[var] == 1)):
                clip_norm = self.GN.clip_norms[var]
                var_norms = T.sum(var_new**2.0, axis=1, keepdims=True)
                var_scale = T.clip(T.sqrt(clip_norm / var_norms), 0., 1.)
                self.gn_updates[var] = var_new * var_scale
            else:
                self.gn_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.gn_updates[var]
        ###################################################
        # Construct updates for the continuous inferencer #
        ###################################################
        for var in self.in_params:
            # these updates are for trainable params in the inferencer net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.joint_cost, var, \
                    consider_constant=[self.GN.dist_mean, self.GN.dist_cov])
            # get the momentum for this var
            var_mom = self.in_moms[var]
            # update the momentum for this var using its grad
            self.in_updates[var_mom] = (self.mo_in[0] * var_mom) + \
                    ((1.0 - self.mo_in[0]) * (var_grad**2.0))
            self.joint_updates[var_mom] = self.in_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_in[0] * (var_grad / T.sqrt(var_mom + 1e-1)))
            # apply "norm clipping" if desired
            if ((var in self.IN.clip_params) and \
                    (var in self.IN.clip_norms) and \
                    (self.IN.clip_params[var] == 1)):
                clip_norm = self.IN.clip_norms[var]
                var_norms = T.sum(var_new**2.0, axis=1, keepdims=True)
                var_scale = T.clip(T.sqrt(clip_norm / var_norms), 0., 1.)
                self.in_updates[var] = var_new * var_scale
            else:
                self.in_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.in_updates[var]
        ####################################################
        # Construct updates for the categorical inferencer #
        ####################################################
        for var in self.pn_params:
            # these updates are for trainable params in the inferencer net...
            # first, get gradient of cost w.r.t. var
            var_grad = T.grad(self.joint_cost, var, \
                    consider_constant=[self.GN.dist_mean, self.GN.dist_cov])
            # get the momentum for this var
            var_mom = self.pn_moms[var]
            # update the momentum for this var using its grad
            self.pn_updates[var_mom] = (self.mo_pn[0] * var_mom) + \
                    ((1.0 - self.mo_pn[0]) * (var_grad**2.0))
            self.joint_updates[var_mom] = self.pn_updates[var_mom]
            # make basic update to the var
            var_new = var - (self.lr_pn[0] * (var_grad / T.sqrt(var_mom + 1e-1)))
            # apply "norm clipping" if desired
            if ((var in self.PN.clip_params) and \
                    (var in self.PN.clip_norms) and \
                    (self.PN.clip_params[var] == 1)):
                clip_norm = self.PN.clip_norms[var]
                var_norms = T.sum(var_new**2.0, axis=1, keepdims=True)
                var_scale = T.clip(T.sqrt(clip_norm / var_norms), 0., 1.)
                self.pn_updates[var] = var_new * var_scale
            else:
                self.pn_updates[var] = var_new
            # add this var's update to the joint updates too
            self.joint_updates[var] = self.pn_updates[var]

        # Construct batch-based training functions for the generator and
        # inferer networks, as well as a joint training function.
        #self.train_gn = self._construct_train_gn()
        #self.train_in = self._construct_train_in()
        self.train_joint = self._construct_train_joint()
        return

    def set_gn_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for self.GN updates.
        """
        zero_ary = np.zeros((1,))
        new_lr = zero_ary + learn_rate
        self.lr_gn.set_value(new_lr.astype(theano.config.floatX))
        new_mo = zero_ary + momentum
        self.mo_gn.set_value(new_mo.astype(theano.config.floatX))
        return

    def set_in_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for self.IN updates.
        """
        zero_ary = np.zeros((1,))
        new_lr = zero_ary + learn_rate
        self.lr_in.set_value(new_lr.astype(theano.config.floatX))
        new_mo = zero_ary + momentum
        self.mo_in.set_value(new_mo.astype(theano.config.floatX))
        return

    def set_pn_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for self.PN updates.
        """
        zero_ary = np.zeros((1,))
        new_lr = zero_ary + learn_rate
        self.lr_pn.set_value(new_lr.astype(theano.config.floatX))
        new_mo = zero_ary + momentum
        self.mo_pn.set_value(new_mo.astype(theano.config.floatX))
        return

    def set_all_sgd_params(self, learn_rate=0.02, momentum=0.9):
        """
        Set learning rate and momentum parameter for all updates.
        """
        zero_ary = np.zeros((1,))
        # set learning rates for GN, IN, PN
        new_lr = zero_ary + learn_rate
        self.lr_gn.set_value(new_lr.astype(theano.config.floatX))
        self.lr_in.set_value(new_lr.astype(theano.config.floatX))
        self.lr_pn.set_value(new_lr.astype(theano.config.floatX))
        # set momentums for GN, IN, PN
        new_mo = zero_ary + momentum
        self.mo_gn.set_value(new_mo.astype(theano.config.floatX))
        self.mo_in.set_value(new_mo.astype(theano.config.floatX))
        self.mo_pn.set_value(new_mo.astype(theano.config.floatX))
        return

    def set_lam_nll(self, lam_nll=1.0):
        """
        Set weight for controlling the influence of the data likelihood.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_nll
        self.lam_nll.set_value(new_lam.astype(theano.config.floatX))
        return

    def set_lam_cat(self, lam_cat=0.0):
        """
        Set the strength of semi-supervised classification cost.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_cat
        self.lam_cat.set_value(new_lam.astype(theano.config.floatX))
        return

    def set_lam_kld(self, lam_kld=1.0):
        """
        Set the strength of regularization on KL-divergence for continuous
        posterior variables. When set to 1.0, this reproduces the standard
        role of KL(posterior || prior) in variational learning.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_kld
        self.lam_kld.set_value(new_lam.astype(theano.config.floatX))
        return

    def set_lam_pea(self, lam_pea=0.0):
        """
        Set the strength of PEA regularization on the categorical posterior.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_pea
        self.lam_pea.set_value(new_lam.astype(theano.config.floatX))
        return

    def set_lam_ent(self, lam_ent=0.0):
        """
        Set the strength of entropy regularization on the categorical posterior.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_ent
        self.lam_ent.set_value(new_lam.astype(theano.config.floatX))
        return

    def set_lam_l2w(self, lam_l2w=1e-3):
        """
        Set the relative strength of l2 regularization on network params.
        """
        zero_ary = np.zeros((1,))
        new_lam = zero_ary + lam_l2w
        self.lam_l2w.set_value(new_lam.astype(theano.config.floatX))
        return

    def _construct_data_nll_cost(self, prob_type='bernoulli'):
        """
        Construct the negative log-likelihood part of cost to minimize.
        """
        assert((prob_type == 'bernoulli') or (prob_type == 'gaussian'))
        if (prob_type == 'bernoulli'):
            log_prob_cost = log_prob_bernoulli(self.Xd, self.GN.output)
        else:
            log_prob_cost = log_prob_gaussian(self.Xd, self.GN.output, \
                    le_sigma=1.0)
        nll_cost = -T.sum(log_prob_cost) / self.Xd.shape[0]
        return nll_cost

    def _construct_post_kld_cost(self):
        """
        Construct the posterior KL-d from prior part of cost to minimize.
        """
        kld_cost = T.sum(self.IN.kld_cost) / self.Xd.shape[0]
        return kld_cost

    def _construct_post_cat_cost(self):
        """
        Construct the label-based semi-supervised cost.
        """
        row_idx = T.arange(self.Yd.shape[0])
        row_mask = T.neq(self.Yd, 0).reshape((self.Yd.shape[0], 1))
        wacky_mat = (self.Yp * row_mask) + (1. - row_mask)
        cat_cost = -T.sum(T.log(wacky_mat[row_idx,(self.Yd.flatten()-1)])) \
                / (T.sum(row_mask) + 1e-4)
        return cat_cost

    def _construct_post_pea_cost(self):
        """
        Construct the pseudo-ensemble agreement cost on the approximate
        posteriors over the categorical latent variable.
        """
        pea_cost = T.sum(self.PN.pea_reg_cost) / self.Xd.shape[0]
        return pea_cost

    def _construct_post_ent_cost(self):
        """
        Construct the entropy cost on the categorical posterior.
        """
        ent_cost = T.sum(cat_entropy(self.Yp)) / self.Xd.shape[0]
        return ent_cost

    def _construct_other_reg_cost(self):
        """
        Construct the cost for low-level basic regularization. E.g. for
        applying l2 regularization to the network activations and parameters.
        """
        act_reg_cost = self.IN.act_reg_cost + self.GN.act_reg_cost + \
                self.PN.act_reg_cost
        gp_cost = sum([T.sum(par**2.0) for par in self.gn_params])
        ip_cost = sum([T.sum(par**2.0) for par in self.in_params])
        pp_cost = sum([T.sum(par**2.0) for par in self.pn_params])
        param_reg_cost = self.lam_l2w[0] * (gp_cost + ip_cost + pp_cost)
        other_reg_cost = (act_reg_cost /self.Xd.shape[0]) + param_reg_cost
        return other_reg_cost

    def _construct_train_joint(self):
        """
        Construct theano function to train inferencer and generator jointly.
        """
        outputs = [self.joint_cost, self.data_nll_cost, self.post_kld_cost, \
                self.post_cat_cost, self.post_pea_cost, self.post_ent_cost, \
                self.other_reg_cost]
        func = theano.function(inputs=[ self.Xd, self.Xc, self.Xm, self.Yd ], \
                outputs=outputs, \
                updates=self.joint_updates)
        COMMENT="""
        theano.printing.pydotprint(func, \
            outfile='GIStack1_train_joint.svg', compact=True, format='svg', with_ids=False, \
            high_contrast=True, cond_highlight=None, colorCodes=None, \
            max_label_size=70, scan_graphs=False, var_with_name_simple=False, \
            print_output_file=True, assert_nb_all_strings=-1)
        """
        return func

    def shared_param_clone(self, rng=None, Xd=None, Xc=None, Xm=None, Yd=None):
        """
        Create a "shared-parameter" clone of this GIStack1.

        This can be used for chaining VAEs for BPTT. (and other stuff too)
        """
        clone_gis = GIStack1(rng=rng, Xd=Xd, Yd=Yd, Xc=Xc, Xm=Xm, \
            g_net=self.GN, i_net=self.IN, p_net=self.PN, \
            data_dim=self.data_dim, prior_dim=self.prior_dim, label_dim=self.label_dim, \
            batch_size=self.batch_size, params=self.params, \
            shared_param_dicts=self.shared_param_dicts)
        return clone_gis

    def sample_gis_from_data(self, X_d, loop_iters=5):
        """
        Sample for several rounds through the I<->G loop, initialized with the
        the "data variable" samples in X_d.
        """
        data_samples = []
        prior_samples = []
        label_samples = []
        X_c = 0.0 * X_d
        X_m = 0.0 * X_d
        for i in range(loop_iters):
            # record the data samples for this iteration
            data_samples.append(1.0 * X_d)
            # sample from their inferred posteriors
            X_p = self.IN.sample_posterior(X_d, X_c, X_m)
            Y_p = self.PN.sample_posterior(X_p)
            # record the sampled points (in the "prior space")
            prior_samples.append(1.0 * X_p)
            label_samples.append(1.0 * Y_p)
            # get next data samples by transforming the prior-space points
            X_d = self.GN.transform_prior(X_p)
        result = {"data samples": data_samples, "prior samples": prior_samples, \
                "label samples": label_samples}
        return result

    def classification_error(self, X_d, Y_d, samples=20):
        """
        Compute classification error for a set of observations X_d with known
        labels Y_d, based on multiple samples from its continuous posterior
        (computed via self.IN), passed through the categorical inferencer
        (i.e. self.IN).
        """
        # first, convert labels to account for semi-supervised labeling
        Y_mask = 1.0 * (Y_d != 0)
        Y_d = Y_d - 1
        # make a function for computing the raw output of the categorical
        # inferencer (i.e. prior to any softmax)
        func = theano.function([self.Xd, self.Xc, self.Xm], \
            outputs=self.PN.output_proto)
        X_c = 0.0 * X_d
        X_m = 0.0 * X_d
        # compute the expected output for X_d
        Y_p = None
        for i in range(samples):
            if Y_p == None:
                Y_p = func(X_d, X_c, X_m)
            else:
                Y_p += func(X_d, X_c, X_m)
        Y_p = Y_p / float(samples)
        # get the implied class labels
        Y_c = np.argmax(Y_p, axis=1).reshape((Y_d.shape[0],1))
        # compute the classification error for points with valid labels
        err_rate = np.sum(((Y_d != Y_c) * Y_mask)) / np.sum(Y_mask)
        return err_rate

def binarize_data(X):
    """
    Make a sample of bernoulli variables with probabilities given by X.
    """
    X_shape = X.shape
    probs = npr.rand(*X_shape)
    X_binary = 1.0 * (probs < X)
    return X_binary.astype(theano.config.floatX)

if __name__=="__main__":
    from load_data import load_udm, load_udm_ss, load_mnist
    import utils as utils

    # Initialize a source of randomness
    rng = np.random.RandomState(1234)

    # Load some data to train/validate/test with
    sup_count = 1000
    dataset = 'data/mnist.pkl.gz'
    datasets = load_udm_ss(dataset, sup_count, rng, zero_mean=False)
    Xtr_su = datasets[0][0].get_value(borrow=False)
    Ytr_su = datasets[0][1].get_value(borrow=False)
    Xtr_un = datasets[1][0].get_value(borrow=False)
    Ytr_un = datasets[1][1].get_value(borrow=False)
    # get the unlabeled data
    Xtr_un = np.vstack([Xtr_su, Xtr_un]).astype(theano.config.floatX)
    Ytr_un = np.vstack([Ytr_su[:,np.newaxis], Ytr_un[:,np.newaxis]]).astype(np.int32)
    Ytr_un = 0 * Ytr_un
    # get the labeled data
    Xtr_su = Xtr_su.astype(theano.config.floatX)
    Ytr_su = Ytr_su[:,np.newaxis].astype(np.int32)
    # get observations and labels for the validation set
    Xva = datasets[2][0].get_value(borrow=False).astype(theano.config.floatX)
    Yva = datasets[2][1].get_value(borrow=False).astype(np.int32)
    Yva = Yva[:,np.newaxis] # numpy is dumb
    # get size information for the data
    un_samples = Xtr_un.shape[0]
    su_samples = Xtr_su.shape[0]
    va_sample = Xva.shape[0]

    # Construct a GenNet and an InfNet, then test constructor for GIPair.
    # Do basic testing, to make sure classes aren't completely broken.
    Xp = T.matrix('Xp_base')
    Xd = T.matrix('Xd_base')
    Xc = T.matrix('Xc_base')
    Xm = T.matrix('Xm_base')
    Yd = T.icol('Yd_base')
    data_dim = Xtr_un.shape[1]
    label_dim = 10
    prior_dim = 100
    prior_sigma = 2.0
    batch_size = 100
    # Choose some parameters for the generator network
    gn_params = {}
    gn_config = [prior_dim, 800, 800, data_dim]
    gn_params['mlp_config'] = gn_config
    gn_params['activation'] = softplus_actfun
    gn_params['lam_l2a'] = 1e-3
    gn_params['vis_drop'] = 0.0
    gn_params['hid_drop'] = 0.0
    gn_params['bias_noise'] = 0.1
    gn_params['out_noise'] = 0.0
    # choose some parameters for the continuous inferencer
    in_params = {}
    shared_config = [data_dim, (200, 4)]
    top_config = [shared_config[-1], (200, 4), prior_dim]
    in_params['shared_config'] = shared_config
    in_params['mu_config'] = top_config
    in_params['sigma_config'] = top_config
    in_params['activation'] = relu_actfun
    in_params['lam_l2a'] = 1e-3
    in_params['vis_drop'] = 0.0
    in_params['hid_drop'] = 0.0
    in_params['bias_noise'] = 0.1
    in_params['input_noise'] = 0.0
    # choose some parameters for the categorical inferencer
    pn_params = {}
    pc0 = [prior_dim, (200, 4), (200, 4), label_dim]
    pn_params['proto_configs'] = [pc0]
    # Set up some spawn networks
    sc0 = {'proto_key': 0, 'input_noise': 0.0, 'bias_noise': 0.0, 'do_dropout': True}
    sc1 = {'proto_key': 0, 'input_noise': 0.0, 'bias_noise': 0.0, 'do_dropout': True}
    pn_params['spawn_configs'] = [sc0, sc1]
    pn_params['spawn_weights'] = [0.5, 0.5]
    # Set remaining params
    pn_params['activation'] = relu_actfun
    pn_params['ear_type'] = 6
    pn_params['lam_l2a'] = 1e-3
    pn_params['vis_drop'] = 0.0
    pn_params['hid_drop'] = 0.5

    # Initialize the base networks for this GIPair
    GN = GenNet(rng=rng, Xp=Xp, prior_sigma=prior_sigma, \
            params=gn_params, shared_param_dicts=None)
    IN = InfNet(rng=rng, Xd=Xd, Xc=Xc, Xm=Xm, prior_sigma=prior_sigma, \
            params=in_params, shared_param_dicts=None)
    PN = PeaNet(rng=rng, Xd=Xd, params=pn_params)
    # Initialize biases in GN, IN, and PN
    GN.init_biases(0.1)
    IN.init_biases(0.1)
    PN.init_biases(0.1)
    # Initialize the GIStack1
    GIS = GIStack1(rng=rng, \
            Xd=Xd, Yd=Yd, Xc=Xc, Xm=Xm, \
            g_net=GN, i_net=IN, p_net=PN, \
            data_dim=data_dim, prior_dim=prior_dim, \
            label_dim=label_dim, batch_size=batch_size, \
            params={}, shared_param_dicts=None)
    # set weighting parameters for the various costs...
    GIS.set_lam_nll(1.0)
    GIS.set_lam_kld(1.0)
    GIS.set_lam_cat(0.0)
    GIS.set_lam_pea(0.0)
    GIS.set_lam_ent(0.0)
    GIS.set_lam_l2w(1e-3)
    # Set initial learning rate and basic SGD hyper parameters
    learn_rate = 0.005
    GIS.set_all_sgd_params(learn_rate=learn_rate, momentum=0.95)

    for i in range(750000):
        scale = 1.0
        if (i < 25000):
            scale = float(i+1) / 25000.0
        if ((i+1 % 100000) == 0):
            learn_rate = learn_rate * 0.75
        # do a minibatch update using unlabeled data
        if True:
            # get some data to train with
            un_idx = npr.randint(low=0,high=un_samples,size=(batch_size,))
            Xd_un = binarize_data(Xtr_un.take(un_idx, axis=0))
            Yd_un = Ytr_un.take(un_idx, axis=0)
            Xc_un = 0.0 * Xd_un
            Xm_un = 0.0 * Xd_un
            # do a minibatch update of the model, and compute some costs
            GIS.set_all_sgd_params(learn_rate=(scale*learn_rate), momentum=0.95)
            GIS.set_lam_nll(1.0)
            GIS.set_lam_kld((scale**2.0) * 1.0)
            GIS.set_lam_cat(0.0)
            GIS.set_lam_pea((scale**2.0) * 1.0)
            GIS.set_lam_ent(0.0)
            outputs = GIS.train_joint(Xd_un, Xc_un, Xm_un, Yd_un)
            joint_cost = 1.0 * outputs[0]
            data_nll_cost = 1.0 * outputs[1]
            post_kld_cost = 1.0 * outputs[2]
            post_cat_cost = 1.0 * outputs[3]
            post_pea_cost = 1.0 * outputs[4]
            post_ent_cost = 1.0 * outputs[5]
            other_reg_cost = 1.0 * outputs[6]
        # do another minibatch update incorporating label information
        if True:
            # get some data to train with
            su_idx = npr.randint(low=0,high=su_samples,size=(batch_size,))
            Xd_su = binarize_data(Xtr_su.take(su_idx, axis=0))
            Yd_su = Ytr_su.take(su_idx, axis=0)
            Xc_su = 0.0 * Xd_su
            Xm_su = 0.0 * Xd_su
            # update only based on the label-based classification cost
            GIS.set_all_sgd_params(learn_rate=(scale*learn_rate), momentum=0.95)
            GIS.set_lam_nll(0.0)
            GIS.set_lam_kld(0.0)
            GIS.set_lam_cat((scale**0.0) * 1.0)
            GIS.set_lam_pea(0.0)
            GIS.set_lam_ent(0.0)
            outputs = GIS.train_joint(Xd_su, Xc_su, Xm_su, Yd_su)
            post_cat_cost = 1.0 * outputs[3]
        if ((i % 500) == 0):
            print("batch: {0:d}, joint_cost: {1:.4f}, nll: {2:.4f}, kld: {3:.4f}, cat: {4:.4f}, pea: {5:.4f}, ent: {6:.4f}, other_reg: {7:.4f}".format( \
                    i, joint_cost, data_nll_cost, post_kld_cost, post_cat_cost, post_pea_cost, post_ent_cost, other_reg_cost))
            if ((i % 1000) == 0):
                # check classification error on training and validation set
                train_err = GIS.classification_error(Xtr_su, Ytr_su, samples=15)
                va_err = GIS.classification_error(Xva, Yva, samples=15)
                print("    tr_err: {0:.4f}, va_err: {1:.4f}".format(train_err, va_err))
        if ((i % 5000) == 0):
            file_name = "GIS_SAMPLES_b{0:d}.png".format(i)
            Xd_samps = np.repeat(Xd_un[0:10,:], 3, axis=0)
            sample_lists = GIS.sample_gis_from_data(Xd_samps, loop_iters=10)
            Xs = np.vstack(sample_lists["data samples"])
            utils.visualize_samples(Xs, file_name)

    print("TESTING COMPLETE!")




##############
# EYE BUFFER #
##############

