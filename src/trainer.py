import os
import itertools
import time
import multiprocessing as mp
from math import inf

import numpy as np
import torch
import torch.nn as nn
import torch.distributed as dist
from tensorboardX import SummaryWriter

from data import Corpus
from model import make_model
from decode import GreedyDecoder
from eval import evalb
from utils import Timer, get_folders, write_args


class Trainer:
    """Trainer for RNNG."""
    def __init__(
        self,
        model=None,
        optimizer=None,
        scheduler=None,
        lr=None,
        step_decay=False,
        learning_rate_warmup_steps=None,
        train_dataset=[],
        dev_dataset=[],
        test_dataset=[],
        batch_size=1,
        num_procs=1,
        elbo_objective=False,
        max_epochs=inf,
        max_time=inf,
        name=None,
        checkpoint_dir=None,
        output_dir=None,
        data_dir=None,
        evalb_dir=None,
        log_dir=None,
        max_grad_norm=5.0,
        print_every=100,
        device=None,
        args=None,  # Used for saving model.
    ):

        self.args = args
        self.model = model
        self.dictionary = model.dictionary
        self.optimizer = optimizer
        self.scheduler = scheduler
        self.batch_size = batch_size
        self.device = device

        self.lr = lr
        self.elbo_objective = elbo_objective
        self.step_decay = step_decay
        self.learning_rate_warmup_steps = learning_rate_warmup_steps
        self.max_grad_norm = max_grad_norm

        self.num_procs = mp.cpu_count() if num_procs == -1 else num_procs
        self.distributed = (self.num_procs > 1)
        self.print_every = print_every
        self.max_epochs = max_epochs
        self.max_time = max_time

        self.train_dataset = train_dataset
        self.dev_dataset = dev_dataset
        self.test_dataset = test_dataset

        self.name = name
        self.data_dir = data_dir
        self.log_dir = log_dir
        self.output_dir = output_dir
        self.checkpoint_dir = checkpoint_dir
        self.evalb_dir = evalb_dir
        self.construct_paths()

        self.losses = []
        self.num_updates = 0
        self.current_dev_fscore = -inf
        self.best_dev_fscore = -inf
        self.best_dev_epoch = 0
        self.test_fscore = -inf

        self.timer = Timer()
        self.tensorboard_writer = SummaryWriter(log_dir)

    def construct_paths(self):
        self.checkpoint_path = os.path.join(self.checkpoint_dir, 'model.pt')
        self.loss_path = os.path.join(self.log_dir, 'loss.csv')

        self.dev_pred_path = os.path.join(self.output_dir, f'{self.name}.dev.pred.trees')
        self.dev_gold_path = os.path.join(self.data_dir, 'dev', f'{self.name}.dev.trees')
        self.dev_result_path = os.path.join(self.output_dir, f'{self.name}.dev.result')

        self.test_pred_path = os.path.join(self.output_dir, f'{self.name}.test.pred.trees')
        self.test_gold_path = os.path.join(self.data_dir, 'test', f'{self.name}.test.trees')
        self.test_result_path = os.path.join(self.output_dir, f'{self.name}.test.result')

    def train(self):
        """Train the model.

        At any point you can hit Ctrl + C to break out of training early.
        """
        try:
            print('Training...')
            # No upper limit of epochs.
            for epoch in itertools.count(start=1):
                if epoch > self.max_epochs:
                    break

                self.current_epoch = epoch

                # Shuffle batches each epoch.
                np.random.shuffle(self.train_dataset)

                # Train one epoch.
                self.train_epoch()

                print('Evaluating fscore on development set...')
                self.check_dev()
                # Scheduler for learning rate.
                if self.step_decay:
                    if (self.num_updates // self.batch_size + 1) > self.learning_rate_warmup_steps:
                        self.scheduler.step(self.best_dev_fscore)

                print('-'*99)
                print(
                    f'| End of epoch {epoch:3d}/{self.max_epochs} '
                    f'| elapsed {self.timer.format_elapsed()} '
                    f'| dev-fscore {self.current_dev_fscore:4.2f} '
                    f'| best dev-epoch {self.best_dev_epoch} '
                    f'| best dev-fscore {self.best_dev_fscore:4.2f} ',
                )
                print('-'*99)
        except KeyboardInterrupt:
            print('-'*99)
            print('Exiting from training early.')
            # Save the losses for plotting and diagnostics
            self.write_losses()
            print('Evaluating fscore on development set...')
            self.check_dev()

        print('Evaluating fscore on test set...')
        test_fscore = self.check_test()
        print('-'*99)
        print(
             f'| End of training '
             f'| best dev-epoch {self.best_dev_epoch:2d} '
             f'| best dev-fscore {self.best_dev_fscore:4.2f} '
             f'| test-fscore {test_fscore}'
        )
        print('-'*99)
        # Save model again but with test fscore information.
        self.test_fscore = test_fscore
        self.save_checkpoint()

    def train_epoch(self):
        if self.distributed:
            self.train_epoch_distributed()
        else:
            self.train_epoch_sequential()

    def train_epoch_distributed(self):
        def init_processes(fn, *args, backend='tcp'):
            """Initialize the distributed environment."""
            os.environ['MASTER_ADDR'] = '127.0.0.1'
            os.environ['MASTER_PORT'] = '29500'
            rank, size, *rest = args
            dist.init_process_group(backend, rank=rank, world_size=size)
            fn(*args)

        def average_gradients(model, size):
            """Gradient averaging."""
            for param in model.parameters():
                if param is not None and param.requires_grad:  # some layers of model are not used and have no grad
                    # dist.all_reduce(param.grad.data, op=dist.reduce_op.SUM)
                    # param.grad.data /= size
                    dist.all_reduce(param.grad.data, op=dist.reduce_op.SUM)
                    param.grad /= size

        def worker(rank, size, model, batches, optimizer, elbo_objective, return_dict, print_every):
            """Distributed training."""
            def clock_time(s):
                h, s = divmod(s, 3600)
                m, s = divmod(s, 60)
                return int(h), int(m), int(s)

            # Restrict each processor to use only 1 thread.
            torch.set_num_threads(1)  # Without this it won't work on Lisa (and slow down on laptop)!
            num_steps = len(batches) // size
            losses = []
            start_time = time.time()
            t0 = time.time()
            chunk_size = len(batches) // dist.get_world_size()
            start, stop = rank*chunk_size, (rank+1)*chunk_size
            for i, batch in enumerate(batches[start:stop], 1):
                sentence, actions = batch
                loss = model(sentence, actions)
                optimizer.zero_grad()
                loss.backward()
                nn.utils.clip_grad_norm_(model.parameters(), max_norm=5.0)
                average_gradients(model, size)
                optimizer.step()

                # Compute the average loss for logging.
                dist.all_reduce(loss, op=dist.reduce_op.SUM)  # inplace operation
                losses.append(loss.data.item() / size)

                if i % print_every == 0 and rank == 0:
                    sents_per_sec = print_every * size / (time.time() - t0)
                    t0 = time.time()
                    message = 'step {}/{} ({:.0f}%) | loss {:.3f} | {:.3f} sents/sec | elapsed {}h{:02}m{:02}s | eta {}h{:02}m{:02}s'.format(
                            i, num_steps, i/num_steps * 100,
                            np.mean(losses[-print_every:]),
                            sents_per_sec,
                            *clock_time(time.time() - start_time),
                            *clock_time((len(batches) - i*size) / sents_per_sec))
                    if elbo_objective:
                        message += (f'| alpha {model.criterion.annealer._alpha:.3f} ' +
                            f'| temp {model.stack.encoder.composition.annealer._temp:.3f} ')
                    print(message)
                    # Save model every now and then.
                    return_dict['model'] = model
            # Save model when finished.
            if rank == 0:
                return_dict['model'] = model

        manager = mp.Manager()
        return_dict = manager.dict()
        processes = []
        for rank in range(self.num_procs):
            args = (rank, self.num_procs, self.model, self.train_dataset,
                self.optimizer, self.elbo_objective, return_dict, self.print_every)
            p = mp.Process(
                target=init_processes,
                args=(worker, *args)
            )
            p.start()
            processes.append(p)
        for p in processes:
            p.join()
        # Catch trained model.
        self.model = return_dict['model']

    def train_epoch_sequential(self):
        """One epoch of training."""
        self.model.train()
        train_timer = Timer()
        num_sentences = len(self.train_dataset)
        num_batches = num_sentences // self.batch_size
        processed = 0

        # if args.memory_debug:
        #     prev_mem = 0
        #     prev_num_objects, prev_num_tensors, prev_num_strings = 0, 0, 0
        #     old_tensors = []

        batches = self.batchify(self.train_dataset)
        for step, minibatch in enumerate(batches, 1):
            if train_timer.elapsed() > self.max_time:
                break

            # Set learning rate.
            self.num_updates += 1
            processed += self.batch_size
            self.schedule_lr()

            # Compute loss over minibatch.
            loss = torch.zeros(1).to(self.device)
            for batch in minibatch:
                sentence, actions = batch
                loss += self.model(sentence, actions)
            loss /= self.batch_size

            self.optimizer.zero_grad()
            loss.backward()
            torch.nn.utils.clip_grad_norm_(self.model.parameters(), self.max_grad_norm)
            self.optimizer.step()

            self.losses.append(loss.item())

            ## NaN debugging
            if torch.isnan(loss.data):
                with open('checkpoints/nan-model.pt', 'wb') as f:
                    torch.save(self.model, f)
                for param in self.model.parameters():
                    if torch.isnan(param.data).sum() > 0:
                        print(param)
                        print()
            ##

            if step % self.print_every == 0:
                # Log to tensorboard.
                self.tensorboard_writer.add_scalar(
                    'Train/Loss', loss.item(), self.num_updates)
                self.tensorboard_writer.add_scalar(
                    'Train/Learning-rate', self.get_lr(), self.num_updates)
                percentage = step / num_batches * 100
                avg_loss = np.mean(self.losses[-self.print_every:])
                lr = self.get_lr()
                sents_per_sec = processed / train_timer.elapsed()
                eta = (num_sentences - processed) / sents_per_sec

                if self.elbo_objective:
                    message = (
                        f'| step {step:6d}/{num_batches:5d} ({percentage:.0f}%) ',
                        f'| neg-elbo {avg_loss:7.3f} ',
                        f'| loss {np.mean(self.model.criterion._train_losses[-self.print_every:]):.3f} ',
                        f'| kl {np.mean(self.model.criterion._train_kl[-self.print_every:]):.3f} ',
                        f'| lr {lr:.1e} ',
                        f'| alpha {self.model.criterion.annealer._alpha:.3f} ',
                        f'| temp {self.model.stack.encoder.composition.annealer._temp:.3f} '
                        f'| {sents_per_sec:4.1f} sents/sec ',
                        f'| elapsed {train_timer.format(train_timer.elapsed())} ',
                        f'| eta {train_timer.format(eta)} '
                    )
                else:
                    message = (
                        f'| step {step:6d}/{num_batches:5d} ({percentage:.0f}%) ',
                        f'| loss {avg_loss:7.3f} ',
                        f'| lr {lr:.1e} ',
                        f'| {sents_per_sec:4.1f} sents/sec ',
                        f'| elapsed {train_timer.format(train_timer.elapsed())} ',
                        f'| eta {train_timer.format(eta)} '
                    )
                print(''.join(message))

    def schedule_lr(self):
        warmup_coeff = self.lr / self.learning_rate_warmup_steps
        if self.num_updates <= self.learning_rate_warmup_steps:
            for param_group in self.optimizer.param_groups:
                param_group['lr'] = self.num_updates * warmup_coeff

    def get_lr(self):
        for param_group in self.optimizer.param_groups:
            return param_group['lr']

    def batchify(self, data):
        def ceil_div(a, b):
            return ((a - 1) // b) + 1

        batches = [data[i*self.batch_size:(i+1)*self.batch_size]
            for i in range(ceil_div(len(data), self.batch_size))]
        return batches

    def save_checkpoint(self):
        with open(self.checkpoint_path, 'wb') as f:
            state = {
                'args': self.args,
                'model': self.model,
                'dictionary': self.dictionary,
                'optimizer': self.optimizer,
                'scheduler': self.scheduler,
                'epochs': self.current_epoch,
                'num-updates': self.num_updates,
                'best-dev-fscore': self.best_dev_fscore,
                'best-dev-epoch': self.best_dev_epoch,
                'test-fscore': self.test_fscore
            }
            torch.save(state, f)

    def predict(self, batches):
        """Use current model to predict trees for batches using greedy decoding."""
        decoder = GreedyDecoder(
            model=self.model, dictionary=self.dictionary, use_chars=self.dictionary.use_chars)
        trees = []
        for i, batch in enumerate(batches):
            sentence, actions = batch
            tree, *rest = decoder(sentence)
            trees.append(tree)
            if i % 10 == 0:
                print(f'Predicting sentence {i}/{len(batches)}...', end='\r')
        return trees

    def check_dev(self):
        """Evaluate the current model on the test dataset."""
        if self.args.model == 'gen':
            return 0
        self.model.eval()
        # Predict trees.
        trees = self.predict(self.dev_dataset)
        with open(self.dev_pred_path, 'w') as f:
            print('\n'.join([tree.linearize() for tree in trees]), file=f)
        # Compute f-score.
        dev_fscore = evalb(
            self.evalb_dir, self.dev_pred_path, self.dev_gold_path, self.dev_result_path)
        # Log score to tensorboard.
        self.tensorboard_writer.add_scalar('Dev/Fscore', dev_fscore, self.num_updates)
        self.current_dev_fscore = dev_fscore
        if dev_fscore > self.best_dev_fscore:
            print(f'Saving new best model to `{self.checkpoint_path}`...')
            self.best_dev_epoch = self.current_epoch
            self.best_dev_fscore = dev_fscore
            self.save_checkpoint()
        return dev_fscore

    def check_test(self):
        """Evaluate the model with best development f-score on the test dataset."""
        if self.args.model == 'gen':
            return 0
        print(f'Loading best saved model from `{self.checkpoint_path}` '
              f'(epoch {self.best_dev_epoch}, fscore {self.best_dev_fscore})...')
        with open(self.checkpoint_path, 'rb') as f:
            state = torch.load(f)
            self.model = state['model']
        # Predict trees.
        trees = self.predict(self.test_dataset)
        with open(self.test_pred_path, 'w') as f:
            print('\n'.join([tree.linearize() for tree in trees]), file=f)
        # Compute f-score.
        test_fscore = evalb(
            self.evalb_dir, self.test_pred_path, self.test_gold_path, self.test_result_path)
        return test_fscore

    def write_losses(self):
        with open(self.loss_path, 'w') as f:
            print('loss', file=f)
            for loss in self.losses:
                print(loss, file=f)
