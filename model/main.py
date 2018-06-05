import cPickle as pickle
import gzip
import json
import os
import time
import random
from datetime import datetime

import numpy as np
import sklearn.metrics as sk
import theano

import myio
import summarization_args

from nn.optimization import create_optimization_updates
from nn.generator import Generator
from nn.encoder import Encoder
from util import say


class Model(object):
    def __init__(self, args, embedding_layer, nclasses):
        self.args = args
        self.embedding_layer = embedding_layer
        self.nclasses = nclasses

    def ready(self):
        args, embedding_layer, nclasses = self.args, self.embedding_layer, self.nclasses

        self.generator = Generator(args, embedding_layer)
        self.encoder = Encoder(args, nclasses, self.generator)

        self.generator.ready()
        self.encoder.ready()

        self.dropout = self.generator.dropout
        self.x = self.generator.x
        self.fw_mask = self.generator.fw_mask
        self.chunk_sizes = self.generator.chunk_sizes

        self.y = self.encoder.y
        self.bm = self.encoder.bm
        self.gold_standard_entities = self.encoder.gold_standard_entities

        self.z = self.generator.z_pred

        self.params = self.encoder.params + self.generator.params

    def ready_test(self):
        args, embedding_layer = self.args, self.embedding_layer
        self.generator = Generator(args, embedding_layer)

        self.generator.ready()
        self.dropout = self.generator.dropout

        self.x = self.generator.x
        self.fw_mask = self.generator.fw_mask
        self.chunk_sizes = self.generator.chunk_sizes
        self.z = self.generator.non_sampled_zpred

    def ready_pretrain(self):
        args, embedding_layer = self.args, self.embedding_layer
        self.generator = Generator(args, embedding_layer)

        self.generator.ready()
        self.generator.pretrain()

        self.dropout = self.generator.dropout
        self.x = self.generator.x
        self.bm = self.generator.bm
        self.fw_mask = self.generator.fw_mask
        self.chunk_sizes = self.generator.chunk_sizes

        self.z = self.generator.non_sampled_zpred
        self.params = self.generator.params

    def save_model(self, path, args, pretrain=False):
        # append file suffix
        if not path.endswith(".pkl.gz"):
            if path.endswith(".pkl"):
                path += ".gz"
            else:
                path += ".pkl.gz"

        # output to path
        with gzip.open(path, "wb") as fout:

            if pretrain:
                pickle.dump(
                    ([x.get_value() for x in self.generator.params],  # generator
                     self.nclasses,
                     args  # training configuration
                     ),
                    fout,
                    protocol=pickle.HIGHEST_PROTOCOL
                )
            else:
                pickle.dump(
                    ([x.get_value() for x in self.encoder.params],  # encoder
                     [x.get_value() for x in self.generator.params],  # generator
                     self.nclasses,
                     args  # training configuration
                     ),
                    fout,
                    protocol=pickle.HIGHEST_PROTOCOL
                )
        if args.trained_emb:
            fname = args.trained_emb + ('pretrain/' if pretrain else '') + myio.create_fname_identifier(args) + '.txt'
            ofp = open(fname, 'w+')
            vectors = self.embedding_layer.params[0].get_value()
            emb_len = args.embedding_dim

            for i in xrange(len(self.embedding_layer.lst_words)):
                word = self.embedding_layer.lst_words[i]
                emb = vectors[i]

                ofp.write(word + ' ')

                for v in xrange(emb_len):
                    ofp.write(str(emb[v]))

                    if v == emb_len - 1:
                        ofp.write('\n')
                    else:
                        ofp.write(' ')

            ofp.close()

    def load_model(self, path, test=False):
        if not os.path.exists(path):
            if path.endswith(".pkl"):
                path += ".gz"
            else:
                path += ".pkl.gz"

        with gzip.open(path, "rb") as fin:
            eparams, gparams, nclasses, args = pickle.load(fin)

        # construct model/network using saved configuration
        self.args = args
        self.nclasses = nclasses

        if test:
            self.ready_test()
        else:
            self.ready()
            for x, v in zip(self.encoder.params, eparams):
                x.set_value(v)

        for x, v in zip(self.generator.params, gparams):
            x.set_value(v)

    def load_model_pretrain(self, path):
        if not os.path.exists(path):
            if path.endswith(".pkl"):
                path += ".gz"
            else:
                path += ".pkl.gz"

        with gzip.open(path, "rb") as fin:
            gparams, nclasses, args = pickle.load(fin)

        if self.args.pretrain:
            self.args = args
            self.nclasses = nclasses
            self.ready_pretrain()
        else:
            self.ready()

        for x, v in zip(self.generator.params, gparams):
            x.set_value(v)

    def test(self):
        args = self.args

        test_generator = theano.function(
            inputs=[self.x, self.fw_mask, self.chunk_sizes],
            outputs=self.z,
            updates=self.generator.sample_updates
        )

        self.dropout.set_value(0.0)
        z, x, y, e, sha = self.evaluate_test_data(test_generator)

        myio.save_test_results_rouge(args, z, x, y, e, sha, self.embedding_layer)

    def dev(self):

        eval_generator = theano.function(
            inputs=[self.x, self.bm, self.fw_mask, self.chunk_sizes],
            outputs=[self.z, self.generator.cost_g, self.generator.obj],
            updates=self.generator.sample_updates,
            on_unused_input='ignore'
        )

        self.dropout.set_value(0.0)

        self.evaluate_pretrain_data_rouge(eval_generator)
        myio.get_rouge(self.args)

    def dev_full(self):

        inputs_d = [self.x, self.y, self.bm, self.gold_standard_entities, self.fw_mask, self.chunk_sizes]
        if not args.pad_repeat:
            inputs_d.append(self.encoder.loss_mask)

        eval_generator = theano.function(
            inputs=inputs_d,
            outputs=[self.generator.non_sampled_zpred, self.encoder.obj, self.encoder.loss, self.encoder.preds_clipped],
            updates=self.generator.sample_updates,
            on_unused_input='ignore'
        )

        self.dropout.set_value(0.0)

        dev_obj, dev_z, dev_x, dev_sha, dev_acc = self.evaluate_data(eval_generator)
        myio.save_dev_results(self.args, None, dev_z, dev_x, dev_sha)
        myio.get_rouge(self.args)

    def train(self):
        args = self.args
        dropout = self.dropout
        padding_id = self.embedding_layer.vocab_map["<padding>"]

        updates_e, lr_e, gnorm_e = create_optimization_updates(
            cost=self.encoder.cost_e,
            params=self.encoder.params,
            method=args.learning,
            beta1=args.beta1,
            beta2=args.beta2,
            lr=args.learning_rate
        )[:3]

        updates_g, lr_g, gnorm_g = create_optimization_updates(
            cost=self.encoder.cost_g,
            params=self.generator.params,
            method=args.learning,
            beta1=args.beta1,
            beta2=args.beta2,
            lr=args.learning_rate
        )[:3]

        outputs_d = [self.generator.non_sampled_zpred, self.encoder.obj, self.encoder.loss, self.encoder.preds_clipped]
        outputs_t = [self.encoder.obj, self.encoder.loss, self.z, self.encoder.zsum, self.encoder.zdiff,
                     self.encoder.bigram_loss, self.encoder.loss_vec, self.encoder.cost_logpz, self.encoder.logpz,
                     self.encoder.cost_vec, self.generator.masks, self.encoder.bigram_loss, self.encoder.preds_clipped,
                     self.encoder.alpha, self.encoder.o]

        inputs_d = [self.x, self.y, self.bm, self.gold_standard_entities, self.fw_mask, self.chunk_sizes]
        inputs_t = [self.x, self.y, self.bm, self.gold_standard_entities, self.fw_mask, self.chunk_sizes]

        if not args.pad_repeat:
            inputs_d.append(self.encoder.loss_mask)
            inputs_t.append(self.encoder.loss_mask)

        eval_generator = theano.function(
            inputs=inputs_d,
            outputs=outputs_d,
            updates=self.generator.sample_updates,
            on_unused_input='ignore'
        )

        train_generator = theano.function(
            inputs=inputs_t,
            outputs=outputs_t,
            updates=updates_e.items() + updates_g.items() + self.generator.sample_updates,
            on_unused_input='ignore'
        )

        unchanged = 0
        best_dev = 1e+2
        last_train_avg_cost = None
        last_dev_avg_cost = None
        tolerance = 0.10 + 1e-3
        dropout_prob = np.float64(args.dropout).astype(theano.config.floatX)

        filename = myio.create_json_filename(args)
        ofp_train = open(filename, 'w+')
        json_train = dict()

        for epoch in xrange(args.max_epochs):
            unchanged += 1
            more_count = 0

            say("Unchanged : {}\n".format(unchanged))

            if unchanged > 25:
                break

            more = True
            if args.decay_lr:
                param_bak = [p.get_value(borrow=False) for p in self.params]

            while more:
                train_cost = 0.0
                train_loss = 0.0
                p1 = 0.0
                more_count += 1

                if more_count > 5:
                    break
                start_time = time.time()

                loss_all = []
                obj_all = []
                zsum_all = []
                bigram_loss_all = []
                loss_vec_all = []
                z_diff_all = []
                cost_logpz_all = []
                logpz_all = []
                z_pred_all = []
                cost_vec_all = []
                train_acc = []

                num_files = args.num_files_train
                N = args.online_batch_size * num_files

                for i in xrange(num_files):
                    if args.pad_repeat:
                        train_batches_x, train_batches_y, train_batches_e, train_batches_bm, _, train_batches_fw, train_batches_csz = myio.load_batches(
                            args.batch_dir + args.source + 'train', i)
                    else:
                        train_batches_x, train_batches_y, train_batches_e, train_batches_bm, train_batches_blm, _, train_batches_fw, train_batches_csz = myio.load_batches(
                            args.batch_dir + args.source + 'train', i)

                    cur_len = len(train_batches_x)

                    random.seed(datetime.now())
                    perm2 = range(cur_len)
                    random.shuffle(perm2)

                    train_batches_x = [train_batches_x[k] for k in perm2]
                    train_batches_y = [train_batches_y[k] for k in perm2]
                    train_batches_e = [train_batches_e[k] for k in perm2]
                    train_batches_bm = [train_batches_bm[k] for k in perm2]
                    train_batches_fw = [train_batches_fw[k] for k in perm2]
                    train_batches_csz = [train_batches_csz[k] for k in perm2]

                    if not args.pad_repeat:
                        train_batches_blm = [train_batches_blm[k] for k in perm2]

                    for j in xrange(cur_len):
                        if args.full_test:
                            if (i* args.online_batch_size + j + 1) % 10 == 0:
                                say("\r{}/{} {:.2f}       ".format(i* args.online_batch_size + j + 1, N, p1 / (i * args.online_batch_size + j + 1)))
                        elif (i* args.online_batch_size + j + 1) % 10 == 0:
                                say("\r{}/{} {:.2f}       ".format(i* args.online_batch_size + j + 1, N, p1 / (i * args.online_batch_size + j + 1)))

                        if args.pad_repeat:
                            bx, by, be, bm, bfw, bcsz = train_batches_x[j], train_batches_y[j], train_batches_e[j], \
                                                  train_batches_bm[j], train_batches_fw[j], train_batches_csz[j]
                            cost, loss, z, zsum, zdiff, bigram_loss, loss_vec, cost_logpz, logpz, cost_vec, masks, bigram_loss, preds_tr = train_generator(
                                bx, by, bm, be, bfw, bcsz)
                        else:
                            bx, by, be, bm, blm, bfw, bcsz = train_batches_x[j], train_batches_y[j], train_batches_e[j], \
                                                  train_batches_bm[j], train_batches_blm[j], train_batches_fw[j], train_batches_csz[j]

                            cost, loss, z, zsum, zdiff, bigram_loss, loss_vec, cost_logpz, logpz, cost_vec, masks, bigram_loss, preds_tr, alpha, o = train_generator(
                                bx, by, bm, be, bfw, bcsz, blm)

                        mask = bx != padding_id

                        train_acc.append(self.eval_acc(be, preds_tr))
                        obj_all.append(cost)
                        loss_all.append(loss)
                        zsum_all.append(np.mean(zsum))
                        loss_vec_all.append(np.mean(loss_vec))
                        z_diff_all.append(np.mean(zdiff))
                        cost_logpz_all.append(np.mean(cost_logpz))
                        logpz_all.append(np.mean(logpz))
                        z_pred_all.append(np.mean(np.sum(z, axis=0)))
                        cost_vec_all.append(np.mean(cost_vec))
                        bigram_loss_all.append(np.mean(bigram_loss))

                        train_cost += cost
                        train_loss += loss

                        p1 += np.sum(z * mask) / (np.sum(mask) + 1e-8)

                cur_train_avg_cost = train_cost / N

                if args.dev:
                    self.dropout.set_value(0.0)
                    dev_obj, dev_z, dev_x, dev_sha, dev_acc = self.evaluate_data(eval_generator)
                    self.dropout.set_value(dropout_prob)
                    cur_dev_avg_cost = dev_obj

                more = False

                if args.decay_lr and last_train_avg_cost is not None:
                    if cur_train_avg_cost > last_train_avg_cost * (1 + tolerance):
                        more = True
                        say("\nTrain cost {} --> {}\n".format(
                            last_train_avg_cost, cur_train_avg_cost
                        ))
                    if args.dev and cur_dev_avg_cost > last_dev_avg_cost * (1 + tolerance):
                        more = True
                        say("\nDev cost {} --> {}\n".format(
                            last_dev_avg_cost, cur_dev_avg_cost
                        ))

                if more:
                    lr_val = lr_g.get_value() * 0.5
                    lr_val = np.float64(lr_val).astype(theano.config.floatX)
                    lr_g.set_value(lr_val)
                    lr_e.set_value(lr_val)
                    say("Decrease learning rate to {}\n".format(float(lr_val)))
                    for p, v in zip(self.params, param_bak):
                        p.set_value(v)
                    continue

                myio.record_observations_verbose(json_train, epoch + 1, loss_all, obj_all, zsum_all, loss_vec_all,
                                             z_diff_all, cost_logpz_all, logpz_all, z_pred_all, cost_vec_all, bigram_loss_all, dev_acc, np.mean(train_acc))

                last_train_avg_cost = cur_train_avg_cost

                say("\n")
                say(("Generator Epoch {:.2f}  costg={:.4f}  lossg={:.4f}  " +
                     "\t[{:.2f}m / {:.2f}m]\n").format(
                    epoch,
                    train_cost / N,
                    train_loss / N,
                    (time.time() - start_time) / 60.0,
                    (time.time() - start_time) / 60.0 / (i * args.online_batch_size + j + 1) * N
                ))

                if args.dev:
                    last_dev_avg_cost = cur_dev_avg_cost
                    if dev_obj < best_dev:
                        best_dev = dev_obj
                        unchanged = 0
                        if args.save_model:
                            filename = args.save_model + myio.create_fname_identifier(args)
                            self.save_model(filename, args)
                            json_train['BEST_DEV_EPOCH'] = epoch

                            if args.sent_level_c:
                                myio.save_dev_results_s(self.args, None, dev_z, dev_x, dev_sha)
                            else:
                                myio.save_dev_results(self.args, None, dev_z, dev_x, dev_sha)

            if more_count > 5:
                json_train['ERROR'] = 'Stuck reducing error rate, at epoch ' + str(epoch + 1) + '. LR = ' + str(lr_val)
                json.dump(json_train, ofp_train)
                ofp_train.close()
                return

        if unchanged > 20:
            json_train['UNCHANGED'] = unchanged

        json.dump(json_train, ofp_train)
        ofp_train.close()

    def pretrain(self):
        args = self.args
        padding_id = self.embedding_layer.vocab_map["<padding>"]

        updates_g, lr_g, gnorm_g = create_optimization_updates(
            cost=self.generator.cost_g,
            params=self.generator.params,
            method=args.learning,
            beta1=args.beta1,
            beta2=args.beta2,
            lr=args.learning_rate
        )[:3]

        eval_generator = theano.function(
            inputs=[self.x,  self.bm, self.fw_mask, self.chunk_sizes],
            outputs=[self.z, self.generator.cost_g, self.generator.obj],
            updates=self.generator.sample_updates
        )

        train_generator = theano.function(
            inputs=[self.x, self.bm, self.fw_mask, self.chunk_sizes],
            outputs=[self.generator.obj, self.z, self.generator.zsum, self.generator.zdiff,  self.generator.cost_g],
            updates=updates_g.items() + self.generator.sample_updates
        )

        unchanged = 0
        best_dev = 1e+2
        last_train_avg_cost = None
        last_dev_avg_cost = None
        tolerance = 0.10 + 1e-3
        dropout_prob = np.float64(args.dropout).astype(theano.config.floatX)

        filename = myio.create_json_filename(args)
        ofp_train = open(filename, 'w+')
        json_train = dict()

        for epoch in xrange(args.max_epochs):
            unchanged += 1
            more_count = 0

            say("Unchanged : {}\n".format(unchanged))

            if unchanged > 20:
                break

            more = True
            if args.decay_lr:
                param_bak = [p.get_value(borrow=False) for p in self.params]

            while more:
                train_cost = 0.0
                train_loss = 0.0
                p1 = 0.0
                more_count += 1

                if more_count > 5:
                    break
                start_time = time.time()

                obj_all = []
                zsum_all = []
                z_diff_all = []
                z_pred_all = []

                num_files = args.num_files_train
                N = args.online_batch_size * num_files

                for i in xrange(num_files):
                    if args.pad_repeat:
                        train_batches_x, _, _, train_batches_bm,  _, train_batches_fw, train_batches_csz = myio.load_batches(
                            args.batch_dir + args.source + 'train', i)
                    else:
                        train_batches_x, _, _, train_batches_bm, _, _, train_batches_fw, train_batches_csz = myio.load_batches(
                            args.batch_dir + args.source + 'train', i)

                    random.seed(datetime.now())
                    perm2 = range(len(train_batches_x))
                    random.shuffle(perm2)

                    train_batches_x = [train_batches_x[k] for k in perm2]
                    train_batches_bm = [train_batches_bm[k] for k in perm2]
                    train_batches_fw = [train_batches_fw[k] for k in perm2]
                    train_batches_csz = [train_batches_csz[k] for k in perm2]

                    cur_len = len(train_batches_x)

                    for j in xrange(cur_len):
                        if args.full_test:
                            if (i * args.online_batch_size + j + 1) % 10 == 0:
                                say("\r{}/{} {:.2f}       ".format(i * args.online_batch_size + j + 1, N, p1 / (i * args.online_batch_size + j + 1)))
                        elif (i * args.online_batch_size + j + 1) % 10 == 0:
                            say("\r{}/{} {:.2f}       ".format(i * args.online_batch_size + j + 1, N, p1 / (i * args.online_batch_size + j + 1)))

                        bx, bm, bfw, bcz = train_batches_x[j], train_batches_bm[j], train_batches_fw[j], train_batches_csz[j]
                        # print bx.shape, bm.shape
                        mask = bx != padding_id

                        obj, z, zsum, zdiff,cost_g = train_generator(bx, bm, bfw, bcz)
                        zsum_all.append(np.mean(zsum))
                        z_diff_all.append(np.mean(zdiff))
                        z_pred_all.append(np.mean(np.sum(z, axis=0)))
                        obj_all.append(np.mean(obj))

                        train_cost += obj

                        p1 += np.sum(z * mask) / (np.sum(mask) + 1e-8)

                cur_train_avg_cost = train_cost / N

                if args.dev:
                    self.dropout.set_value(0.0)
                    dev_obj, dev_z, x, sha_ls = self.evaluate_pretrain_data(eval_generator)
                    self.dropout.set_value(dropout_prob)
                    cur_dev_avg_cost = dev_obj

                more = False

                if args.decay_lr and last_train_avg_cost is not None:
                    if cur_train_avg_cost > last_train_avg_cost * (1 + tolerance):
                        more = True
                        say("\nTrain cost {} --> {}\n".format(
                            last_train_avg_cost, cur_train_avg_cost
                        ))
                    if args.dev and cur_dev_avg_cost > last_dev_avg_cost * (1 + tolerance):
                        more = True
                        say("\nDev cost {} --> {}\n".format(
                            last_dev_avg_cost, cur_dev_avg_cost
                        ))

                if more:
                    lr_val = lr_g.get_value() * 0.5
                    lr_val = np.float64(lr_val).astype(theano.config.floatX)
                    lr_g.set_value(lr_val)
                    say("Decrease learning rate to {}\n".format(float(lr_val)))
                    for p, v in zip(self.params, param_bak):
                        p.set_value(v)
                    continue

                myio.record_observations_pretrain(json_train, epoch + 1, obj_all, zsum_all, z_diff_all, z_pred_all)

                last_train_avg_cost = cur_train_avg_cost

                say("\n")
                say(("Generator Epoch {:.2f}  costg={:.4f}  lossg={:.4f}  " +
                     "\t[{:.2f}m / {:.2f}m]\n").format(
                    epoch,
                    train_cost / N,
                    train_loss / N,
                    (time.time() - start_time) / 60.0,
                    (time.time() - start_time) / 60.0 / (i * args.online_batch_size + j + 1) * N
                ))

                if args.dev:
                    last_dev_avg_cost = cur_dev_avg_cost
                    if dev_obj < best_dev:
                        best_dev = dev_obj
                        unchanged = 0
                        if args.save_model:
                            filename = self.args.save_model + 'pretrain/' + myio.create_fname_identifier(self.args)
                            self.save_model(filename, self.args, pretrain=True)
                            json_train['BEST_DEV_EPOCH'] = epoch

                            myio.save_dev_results(self.args, None, dev_z, x, sha_ls)

            if more_count > 5:
                json_train['ERROR'] = 'Stuck reducing error rate, at epoch ' + str(epoch + 1) + '. LR = ' + str(lr_val)
                json.dump(json_train, ofp_train)
                ofp_train.close()
                return

        if unchanged > 20:
            json_train['UNCHANGED'] = unchanged

        json.dump(json_train, ofp_train)
        ofp_train.close()

    def evaluate_pretrain_data(self, eval_func):
        tot_obj = 0.0
        N = 0

        dev_z = []
        x = []
        sha_ls = []

        num_files = self.args.num_files_dev

        for i in xrange(num_files):
            if args.pad_repeat:
                batches_x, _, _, batches_bm, batches_sha, batches_rx, batches_fw, batches_cs = myio.load_batches(
                    self.args.batch_dir + self.args.source + 'dev', i)
            else:
                batches_x, _, _, batches_bm, _, batches_sha, batches_rx, batches_fw, batches_cs = myio.load_batches(
                    self.args.batch_dir + self.args.source + 'dev', i)

            cur_len = len(batches_x)

            for j in xrange(cur_len):
                bx, bm, sha, rx, bfw, bcs = batches_x[j], batches_bm[j], batches_sha[j], batches_rx[j], batches_fw[j], batches_cs[j]
                bz, l, o = eval_func(bx, bm, bfw, bcs)
                tot_obj += o
                N += len(bx)

                x.append(rx)
                dev_z.append(bz)
                sha_ls.append(sha)

        return tot_obj / float(N), dev_z, x, sha_ls

    def evaluate_pretrain_data_rouge(self, eval_func):
        tot_obj = 0.0
        N = 0

        dev_z = []
        x = []
        sha_ls = []

        num_files = self.args.num_files_dev

        for i in xrange(num_files):
            batches_x, _, _, batches_bm, batches_sha, batches_rx, batches_fw, batches_cs = myio.load_batches(
                self.args.batch_dir + self.args.source + 'dev', i)

            cur_len = len(batches_x)

            for j in xrange(cur_len):
                bx, bm, sha, rx, bfw, bcs = batches_x[j], batches_bm[j], batches_sha[j], batches_rx[j], batches_fw[j], batches_cs[j]
                bz, l, o = eval_func(bx, bm, bfw, bcs)
                tot_obj += o

                x.append(rx)
                dev_z.append(bz)
                sha_ls.append(sha)

            N += len(batches_x)

        myio.save_dev_results(self.args, None, dev_z, x, sha_ls)

        return tot_obj / float(N), dev_z

    def evaluate_data(self, eval_func):
        tot_obj = 0.0
        N = 0

        dev_z = []
        x = []
        sha_ls = []
        dev_acc = []

        num_files = self.args.num_files_dev

        for i in xrange(num_files):
            if args.pad_repeat:
                batches_x, batches_y, batches_e, batches_bm, batches_sha, batches_rx, batches_fw, batches_csz = myio.load_batches(
                    self.args.batch_dir + self.args.source + 'dev', i)
            else:
                batches_x, batches_y, batches_e, batches_bm, batches_lm,  batches_sha, batches_rx, batches_fw, batches_csz = myio.load_batches(
                    self.args.batch_dir + self.args.source + 'dev', i)

            cur_len = len(batches_x)

            for j in xrange(cur_len):
                if args.pad_repeat:
                    bx, by, be, bm, sha, rx, fw, csz = batches_x[j], batches_y[j], batches_e[j], batches_bm[j], \
                                                       batches_sha[j], batches_rx[j], batches_fw[j], batches_csz[j]
                    bz, o, e, preds = eval_func(bx, by, bm, be, fw, csz)
                else:
                    bx, by, be, bm, sha, rx, ble, fw, csz = batches_x[j], batches_y[j], batches_e[j], batches_bm[j], \
                                                            batches_sha[j], batches_rx[j], batches_lm[j], batches_fw[j], \
                                                            batches_csz[j]
                    bz, o, e, preds = eval_func(bx, by, bm, be, fw, csz, ble)

                tot_obj += o

                x.append(rx)
                dev_z.append(bz)
                sha_ls.append(sha)
                dev_acc.append(self.eval_acc(be, preds))

            N += cur_len

        return tot_obj / float(N), dev_z, x, sha_ls, np.mean(dev_acc)

    def evaluate_test_data(self, eval_func):
        N = 0

        test_z = []
        x = []
        y = []
        e =[]
        sha_ls = []

        num_files = self.args.num_files_test

        for i in xrange(num_files):
            batches_x, batches_y, batches_e, _, batches_sha, batches_rx, batches_fw, batches_csz = myio.load_batches(
                self.args.batch_dir + self.args.source + 'test', i)

            cur_len = len(batches_x)

            for j in xrange(cur_len):
                bx, rx, bsha, by, be, fw, csz = batches_x[j], batches_rx[j], batches_sha[j], batches_y[j], batches_e[j], batches_fw[j], batches_csz[j]
                bz = eval_func(bx, fw, csz)

                x.append(rx)
                y.append(by)
                e.append(be)

                test_z.append(bz)
                sha_ls.append(bsha)

            N += len(batches_x)

        return test_z, x, y, e, sha_ls

    def eval_acc(self,e, preds):
        gs = np.argmax(e, axis=1)
        system = np.argmax(preds, axis=1)

        return sk.accuracy_score(gs, system)


def main():
    assert args.embedding, "Pre-trained word embeddings required."

    vocab, parse_v = myio.get_vocab(args)
    embedding_layer = myio.create_embedding_layer(args, args.embedding, vocab, '<unk>')

    n_classes =args.nclasses

    model = Model(
        args=args,
        embedding_layer=embedding_layer,
        nclasses=n_classes
    )

    if args.dev_baseline:
        num_files = args.num_files_dev

        rx_ls = []
        bm_ls = []

        for i in xrange(num_files):
            batches_x, _, _, batches_bm, _, batches_sha, batches_rx = myio.load_batches(
                args.batch_dir + args.source + 'dev', i)

            cur_len = len(batches_x)

            for j in xrange(cur_len):
                _, bm, _, rx = batches_x[j], batches_bm[j], batches_sha[j], batches_rx[j]
                rx_ls.append(rx)
                bm_ls.append(bm)

        myio.eval_baseline(args, bm_ls, rx_ls, 'dev')
    elif args.test_baseline:
        num_files = args.num_files_test

        rx_ls = []
        bm_ls = []

        for i in xrange(num_files):
            batches_x, batches_bm, batches_sha, batches_rx = myio.load_batches(
                args.batch_dir + args.source + 'test', i)

            cur_len = len(batches_x)

            for j in xrange(cur_len):
                _, bm, _, rx = batches_x[j], batches_bm[j], batches_sha[j], batches_rx[j]
                rx_ls.append(rx)
                bm_ls.append(bm)

        myio.eval_baseline(args, bm_ls, rx_ls, 'test')

    elif args.train:

        if args.pretrain:
            model.ready_pretrain()
            model.pretrain()
        else:
            if args.load_model_pretrain:
                model.load_model_pretrain(args.save_model + 'pretrain/' + args.load_model)
            else:
                model.ready()

            model.train()

    elif args.dev:
        if args.pretrain:
            model.load_model_pretrain(args.save_model + 'pretrain/' + args.load_model)
            model.dev()
        else:
            model.load_model(args.save_model + args.load_model)
            model.dev_full()

    elif args.test:
        model.load_model(args.save_model + args.load_model, True)
        model.test()


if __name__ == "__main__":
    args = summarization_args.get_args()
    main()
