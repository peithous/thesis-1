import os
import itertools

import numpy as np
import torch
from tensorboardX import SummaryWriter

from data import Corpus
from model import make_model
from trainer import Trainer
from eval import evalb
from utils import Timer, write_losses, get_folders, write_args


def schedule_lr(args, optimizer, update):
    update = update + 1
    warmup_coeff = args.lr / args.learning_rate_warmup_steps
    if update <= args.learning_rate_warmup_steps:
        for param_group in optimizer.param_groups:
            param_group['lr'] = update * warmup_coeff


def get_lr(optimizer):
    for param_group in optimizer.param_groups:
        return param_group['lr']


def batchify(batches, batch_size):
    def ceil_div(a, b):
        return ((a - 1) // b) + 1
    return [batches[i*batch_size:(i+1)*batch_size]
            for i in range(ceil_div(len(batches), batch_size))]


def main(args):
    if args.memory_debug:
        from pprint import pprint
        from collections import Counter
        from test_memory import get_added_memory, get_num_objects, print_tensor_increase

    # Set random seeds.
    torch.manual_seed(args.seed)
    np.random.seed(args.seed)

    # Set cuda.
    use_cuda = not args.disable_cuda and torch.cuda.is_available()
    args.device = torch.device("cuda" if use_cuda else "cpu")
    print(f'Device: {args.device}.')

    # Make output folder structure.
    if not args.disable_folders:
        subdir, logdir, checkdir, outdir = get_folders(args)
        os.mkdir(logdir)
        os.mkdir(checkdir)
        os.mkdir(outdir)
        print(f'Output subdirectory: `{subdir}`.')
    else:
        print('Did not make output folders!')

    # Save arguments.
    write_args(args, logdir)

    print(f'Saving logs to `{logdir}`.')
    print(f'Saving predictions to `{outdir}`.')
    print(f'Saving models to `{checkdir}`.')

    print(f'Loading data from `{args.data}`...')
    corpus = Corpus(
        data_path=args.data,
        model=args.model,
        textline=args.textline,
        name=args.name,
        use_chars=args.use_chars,
        max_lines=args.max_lines
    )
    train_dataset = corpus.train.batches(length_ordered=False, shuffle=True)
    dev_dataset = corpus.dev.batches(length_ordered=False, shuffle=False)
    test_dataset = corpus.test.batches(length_ordered=False, shuffle=False)
    print(corpus)

    # Sometimes we don't want to use all data.
    if args.debug:
        print('Debug mode.')
        train_dataset = train_dataset[:20]
        dev_dataset = dev_dataset[:30]
        test_dataset = test_dataset[:30]
    if args.max_lines != -1:
        dev_dataset = dev_dataset[:100]
        test_dataset = test_dataset[:100]

    # Create model.
    model = make_model(args, corpus.dictionary)
    model.to(args.device)

    trainable_parameters = [param for param in model.parameters() if param.requires_grad]
    # Learning rate is set during training by set_lr().
    optimizer = torch.optim.Adam(trainable_parameters, lr=1., betas=(0.9, 0.98), eps=1e-9)
    scheduler = torch.optim.lr_scheduler.ReduceLROnPlateau(
        optimizer, 'max',
        factor=args.step_decay_factor,
        patience=args.step_decay_patience,
        verbose=True,
    )

    elbo_objective = (args.composition in ('latent-factors', 'latent-attention'))

    trainer = Trainer(
        model=model,
        optimizer=optimizer,
        scheduler=scheduler,
        nprocs=args.nprocs,
        lr=args.lr,
        elbo_objective=elbo_objective,
        train_dataset=train_dataset,
        dev_dataset=dev_dataset,
        test_dataset=test_dataset,
        print_every=args.print_every,
        batch_size=args.batch_size,
        max_epochs=args.max_epochs,
        max_time=args.max_time,
        name=args.name,
        checkpoint_dir=checkdir,
        output_dir=outdir,
        data_dir=args.data,
        evalb_dir=args.evalb_dir,
        device=args.device,
        step_decay=args.step_decay,
        learning_rate_warmup_steps=args.learning_rate_warmup_steps,
        max_grad_norm=args.clip,
        args=args,  # Used for saving model.
    )

    trainer.train()

    trainer.check_test()
