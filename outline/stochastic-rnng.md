
# RNNG with stochastic (RNN) decoder

This is the project that Wilker proposed.

## Introduction
This combines [Recurrent Neural Network Grammars](https://arxiv.org/abs/1602.07776) and [A Stochastic Decoder for NMT](https://arxiv.org/abs/1602.07776).

The RNNG is a parsing model that makes no Markov assumption. The RNNG uses as shift-reduce parser (stack, buffer, etc.) where the decisions are parametrized by RNNs that condition on the entire syntactic derivation history. Dyer proposed a discriminative and a generative variant. Parsing models that make no Markov assumption are a good testbed for the stochastic RNN model that Philip and I propose in our ACL submission.

The discriminative model can be used to parse, and is straightforward to train. The generative model is harder to train and uses importance sampling, but can additionally be used as a language model, if you evaluate p(x) by marginalizing over all latent trees that generate x. The RNNG can then be used as a syntactic language model to generate text. **This direction interests me the most**.

The RNN that parametrizes the parse decisions (`gen(x)` and `reduce`) can be replaced by the stochastic RNN, which is trained with VI.

## Related work

* In [Generative Incremental Dependency Parsing with Neural Networks](http://www.aclweb.org/anthology/P15-2142) (Buys and Blunsom 2015) shows how to make a stack-reduce parser generative and parametrize the local decisions on by MLPs that work on feature templates similar to Chen and Manning (2014).
* In [Learning to Parse and Translate Improves Neural Machine Translation](https://arxiv.org/pdf/1702.03525.pdf) the RNNG is used as decoder. This does something. Or not.
* The **SPINN** looks similar. [A Fast Unified Model for Parsing and Sentence Understanding](http://www.foldl.me/uploads/papers/acl2016.pdf), but is mostly concerned with encoding sentences
  > To our knowledge, SPINN is the first model to use this architecture for the purpose of sentence interpretation, rather than parsing or generation.


## Constituency or dependency

The RNNG uses a stack-reduce parser. Hence it can parse to give both constituency and dependency trees, since both types of trees are
represented as a sequence of the three types of actions in a transition-based parsing model. **My in**

## Research questions

1. Can the stochastic decoder make the discriminative parser more robust for out of domain parsing?
2. Can the stochastic decoder let the generative parser create more variable sentences?
3. Can we come up with another way of training the generative parser?
4. Can we use the (stochastic) generative parser as a decoder (conditional language model) for NMT? E.g. [Learning to Parse ]

## Outline

1. Replicate the original paper. Focus on the discriminative variant in the beginning, and then continue to the generative model.
2. Replace the RNNs with SRNNs to create the S-RNNG.
3. Use the generative S-RNNG as a decoder for neural NMT.



## Possible other directions:

Wilker:

> The generative variant is very hard to train (even with variational inference). I find Dyer's strategy unsatisfactory even though seemly effective (the fact that it works also puzzles me a bit).

Can we come up with another method of training the generative parser? This could be an interesting challenge.