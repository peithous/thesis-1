import os

import torch
import numpy as np

def make_gensim_compatible(dim):
    """Make a glove vector path gensim compatible.

    Prints the number of words and dimension at the top of a copy of the glove
    file with dimension dim.
    """
    in_path  = f'glove.6B.{dim}d.txt'
    out_path = f'glove.6B.{dim}d.gensim.txt'
    length = sum(1 for _ in open(in_path))
    with open(in_path) as f:
        with open(out_path, 'w') as g:
            print(length, dim, file=g)
            print(f.read(), file=g, end='')

def load_glove(words, dim, dir, logdir):
    """Loads all the words from the glove vectors of dimension dim in saved in dir."""
    if dir.startswith('~'):
        dir = os.path.expanduser(dir)
    assert dim in (50, 100, 200, 300), f'invalid dim: {dim}, choose from (50, 100, 200, 300).'
    assert os.path.exists(
            os.path.join(dir, f'glove.6B.{dim}d.txt')
        ), 'glove file not availlable.'
    # Load the glove vectors into a dictionary.
    try:
        # Fastest way, if gensim is installed.
        from gensim.models import KeyedVectors
        path = os.path.join(dir, f'glove.6B.{dim}d.gensim.txt')
        # If gensim version does not yet exist, we make it.
        if not os.path.exists(path):
            make_gensim_compatible(dim)
        glove = KeyedVectors.load_word2vec_format(path, binary=False)
    except ImportError:
        # Works always.
        path = os.path.join(dir, f'glove.6B.{dim}d.txt')
        glove = dict()
        with open(path) as f:
            for line in f:
                line = line.strip().split()
                word, vec = line[0], line[1:]
                vec = np.array([float(val) for val in vec])
                glove[word] = vec
    # Get the glove vector for each word in the dictionary and log words not found.
    logfile = open(os.path.join(logdir, 'glove.error.txt'), 'w')
    vectors = []
    for word in words:
        vec = get_vector(word, glove, dim, logfile)
        vectors.append(vec)
    logfile.close()
    vectors = np.vstack(vectors)
    vectors = torch.FloatTensor(vectors)
    return vectors

def get_vector(word, glove, dim, logfile):
    """Get the word from the glove dictionary.

    Tries alternatives if the word is not in the glove dictionary,
    and finally initializes random if even this cannot be done. These words
    are printed to logfile. Default initialization is Normal(0,1).
    """
    try:
        vec = glove[word]
    # Word not found.
    except KeyError:
        # If the word is uppercase we try lowercase.
        if word.lower() in glove:
            vec = glove[word.lower()]
        # If word is can be split using characters like `-` and `&` then
        # we take the average embedding of those splits.
        elif splits(word) is not None: # word can be split into parts
            vec = np.zeros(dim) # accumulator
            parts = splits(word)
            for part in parts:
                if part in glove:
                    vec += glove[part]
                elif part.lower() in glove:
                    vec += glove[part.lower()]
                else:
                    pass # implicit zero vector for this part
            vec /= len(parts) # Average of the embeddings
        # Otherwise we assign a random vector.
        else:
            vec = np.random.randn(dim) # Random vector.
            print(word, file=logfile) # print word to logfile
    return vec

def splits(word, chars=('-', '\/', ',', '.', '&', "'")):
    """Tries to split the word with one of the given characters.

    Returns the longest list of chunks given the characters, and returns None
    if the word cannot split with one of the characters.
    Example:
        `worse-than-expected` returns [`worse`, `then`, `expected`]
        `automobile` -> None
    """
    words = None
    longest = 1
    for char in chars:
        chunks = word.split(char)
        if len(chunks) > longest:
            words = chunks
            longest = len(chunks)
    return words