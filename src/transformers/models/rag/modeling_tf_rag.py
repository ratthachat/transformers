# coding=utf-8
# Copyright 2020, The RAG Authors and The HuggingFace Inc. team.
#
# Licensed under the Apache License, Version 2.0 (the "License");
# you may not use this file except in compliance with the License.
# You may obtain a copy of the License at
#
#     http://www.apache.org/licenses/LICENSE-2.0
#
# Unless required by applicable law or agreed to in writing, software
# distributed under the License is distributed on an "AS IS" BASIS,
# WITHOUT WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied.
# See the License for the specific language governing permissions and
# limitations under the License.

"""TFRAG model implementation. (draft version)"""

from dataclasses import dataclass
from typing import Dict, List, Optional, Tuple

import tensorflow as tf

from ...activations_tf import ACT2FN
from ...configuration_utils import PretrainedConfig
from ...file_utils import (
    ModelOutput,
    add_start_docstrings,
    add_start_docstrings_to_model_forward,
    replace_return_docstrings,
)
from ...generation_tf_utils import *  # this is needed since we adjust _generate_no_beam and _generate_with_beam to TFRag
from ...modeling_tf_outputs import TFBaseModelOutput, TFBaseModelOutputWithPast, TFSeq2SeqLMOutput
from ...modeling_tf_utils import (
    DUMMY_INPUTS,
    TFCausalLanguageModelingLoss,
    TFPreTrainedModel,
    input_processing,
    keras_serializable,
    shape_list,
)
from ...tokenization_utils import BatchEncoding
from ...utils import logging
from .configuration_rag import RagConfig
from .retrieval_rag import RagRetriever


logger = logging.get_logger(__name__)

_CONFIG_FOR_DOC = "RagConfig"


@dataclass
class TFRetrievAugLMMarginOutput(ModelOutput):
    """
    Base class for retriever augmented marginalized models outputs.

    Args:
        loss (:obj:`tf.Tensor` of shape :obj:`(1,)`, `optional`, returned when :obj:`labels` is provided):
            Language modeling loss.
        logits (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length, config.vocab_size)`):
            Prediction scores of the language modeling head. The score is possibly marginalized over all documents for
            each vocabulary token.
        doc_scores (:obj:`tf.Tensor` of shape :obj:`(batch_size, config.n_docs)`):
            Score between each retrieved document embeddings (see :obj:`retrieved_doc_embeds`) and
            :obj:`question_encoder_last_hidden_state`.
        past_key_values (:obj:`List[tf.Tensor]`, `optional`, returned when ``use_cache=True`` is passed or when ``config.use_cache=True``):
            List of :obj:`tf.Tensor` of length :obj:`config.n_layers`, with each tensor of shape :obj:`(2, batch_size,
            num_heads, sequence_length, embed_size_per_head)`).

            Contains precomputed hidden-states (key and values in the attention blocks) of the decoder that can be used
            (see :obj:`past_key_values` input) to speed up sequential decoding.
        retrieved_doc_embeds (:obj:`tf.Tensor` of shape :obj:`(batch_size, config.n_docs, hidden_size)`, `optional`, returned when `output_retrieved=True`):
            Embedded documents retrieved by the retriever. Is used with ``question_encoder_last_hidden_state`` to
            compute the ``doc_scores``.
        retrieved_doc_ids (:obj:`tf.Tensor` (int32) of shape :obj:`(batch_size, config.n_docs)`, `optional`, returned when `output_retrieved=True`):
            The indexes of the embedded documents retrieved by the retriever.
        context_input_ids (:obj:`tf.Tensor`(int32) of shape :obj:`(batch_size * config.n_docs, config.max_combined_length)`, `optional`, returned when `output_retrieved=True`):
            Input ids post-processed from the retrieved documents and the question encoder input_ids by the retriever.
        context_attention_mask (:obj:`tf.Tensor` (int32) of shape :obj:`(batch_size * config.n_docs, config.max_combined_length)`, `optional`, returned when `output_retrieved=True`):
            Attention mask post-processed from the retrieved documents and the question encoder :obj:`input_ids` by the
            retriever.
        question_encoder_last_hidden_state (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
            Sequence of hidden states at the output of the last layer of the question encoder pooled output of the
            model.
        question_enc_hidden_states (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_hidden_states=True`` is passed or when ``config.output_hidden_states=True``):
            Tuple of :obj:`tf.Tensor` (one for the output of the embeddings and one for the output of each layer) of
            shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden states of the question encoder at the output of each layer plus the initial embedding outputs.
        question_enc_attentions (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_attentions=True`` is passed or when ``config.output_attentions=True``):
            Tuple of :obj:`tf.Tensor` (one for each layer) of shape :obj:`(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights of the question encoder, after the attention softmax, used to compute the weighted
            average in the self-attention heads.
        generator_enc_last_hidden_state (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
            Sequence of hidden-states at the output of the last layer of the generator encoder of the model.
        generator_enc_hidden_states (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_hidden_states=True`` is passed or when ``config.output_hidden_states=True``):
            Tuple of :obj:`tf.Tensor` (one for the output of the embeddings and one for the output of each layer) of
            shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden states of the generator encoder at the output of each layer plus the initial embedding outputs.
        generator_enc_attentions (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_attentions=True`` is passed or when ``config.output_attentions=True``):
            Tuple of :obj:`tf.Tensor` (one for each layer) of shape :obj:`(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights of the generator encoder, after the attention softmax, used to compute the weighted
            average in the self-attention heads.
        generator_dec_hidden_states (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_hidden_states=True`` is passed or when ``config.output_hidden_states=True``):
            Tuple of :obj:`tf.Tensor` (one for the output of the embeddings and one for the output of each layer) of
            shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden states of the generator decoder at the output of each layer plus the initial embedding outputs.
        generator_dec_attentions (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_attentions=True`` is passed or when ``config.output_attentions=True``):
            Tuple of :obj:`tf.Tensor` (one for each layer) of shape :obj:`(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights of the generator decoder, after the attention softmax, used to compute the weighted
            average in the self-attention heads.
    """

    loss: Optional[tf.Tensor] = None
    logits: tf.Tensor = None
    doc_scores: tf.Tensor = None
    past_key_values: Optional[List[tf.Tensor]] = None
    retrieved_doc_embeds: Optional[tf.Tensor] = None
    retrieved_doc_ids: Optional[tf.Tensor] = None
    context_input_ids: Optional[tf.Tensor] = None
    context_attention_mask: Optional[tf.Tensor] = None
    question_encoder_last_hidden_state: Optional[tf.Tensor] = None
    question_enc_hidden_states: Optional[Tuple[tf.Tensor]] = None
    question_enc_attentions: Optional[Tuple[tf.Tensor]] = None
    generator_enc_last_hidden_state: Optional[tf.Tensor] = None
    generator_enc_hidden_states: Optional[Tuple[tf.Tensor]] = None
    generator_enc_attentions: Optional[Tuple[tf.Tensor]] = None
    generator_dec_hidden_states: Optional[Tuple[tf.Tensor]] = None
    generator_dec_attentions: Optional[Tuple[tf.Tensor]] = None


@dataclass
class TFRetrievAugLMOutput(ModelOutput):
    """
    Args:
        logits (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length, config.vocab_size)`):
            Prediction scores of the language modeling head. The score is possibly marginalized over all documents for
            each vocabulary token.
        doc_scores (:obj:`tf.Tensor` of shape :obj:`(batch_size, config.n_docs)`):
            Score between each retrieved document embeddings (see :obj:`retrieved_doc_embeds`) and
            :obj:`question_encoder_last_hidden_state`.
        past_key_values (:obj:`List[tf.Tensor]`, `optional`, returned when ``use_cache=True`` is passed or when ``config.use_cache=True``):
            List of :obj:`tf.Tensor` of length :obj:`config.n_layers`, with each tensor of shape :obj:`(2, batch_size,
            num_heads, sequence_length, embed_size_per_head)`).

            Contains precomputed hidden-states (key and values in the attention blocks) of the decoder that can be used
            (see :obj:`past_key_values` input) to speed up sequential decoding.
        retrieved_doc_embeds (:obj:`tf.Tensor` of shape :obj:`(batch_size, config.n_docs, hidden_size)`, `optional`, returned when `output_retrieved=True`):
            Embedded documents retrieved by the retriever. Is used with ``question_encoder_last_hidden_state`` to
            compute the ``doc_scores``.
        retrieved_doc_ids (:obj:`tf.Tensor` of shape :obj:`(batch_size, config.n_docs)`, `optional`, returned when `output_retrieved=True`):
            The indexes of the embedded documents retrieved by the retriever.
        context_input_ids (:obj:`tf.Tensor` of shape :obj:`(batch_size * config.n_docs, config.max_combined_length)`, `optional`, returned when `output_retrieved=True`):
            Input ids post-processed from the retrieved documents and the question encoder input_ids by the retriever.
        context_attention_mask (:obj:`tf.Tensor` of shape :obj:`(batch_size * config.n_docs, config.max_combined_length)`, `optional`, returned when `output_retrieved=True`):
            Attention mask post-processed from the retrieved documents and the question encoder :obj:`input_ids` by the
            retriever.
        question_encoder_last_hidden_state (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
            Sequence of hidden states at the output of the last layer of the question encoder pooled output of the
            model.
        question_enc_hidden_states (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_hidden_states=True`` is passed or when ``config.output_hidden_states=True``):
            Tuple of :obj:`tf.Tensor` (one for the output of the embeddings and one for the output of each layer) of
            shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden states of the question encoder at the output of each layer plus the initial embedding outputs.
        question_enc_attentions (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_attentions=True`` is passed or when ``config.output_attentions=True``):
            Tuple of :obj:`tf.Tensor` (one for each layer) of shape :obj:`(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights of the question encoder, after the attention softmax, used to compute the weighted
            average in the self-attention heads.
        generator_enc_last_hidden_state (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length, hidden_size)`, `optional`):
            Sequence of hidden-states at the output of the last layer of the generator encoder of the model.
        generator_enc_hidden_states (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_hidden_states=True`` is passed or when ``config.output_hidden_states=True``):
            Tuple of :obj:`tf.Tensor` (one for the output of the embeddings and one for the output of each layer) of
            shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden states of the generator encoder at the output of each layer plus the initial embedding outputs.
        generator_enc_attentions (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_attentions=True`` is passed or when ``config.output_attentions=True``):
            Tuple of :obj:`tf.Tensor` (one for each layer) of shape :obj:`(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights of the generator encoder, after the attention softmax, used to compute the weighted
            average in the self-attention heads.
        generator_dec_hidden_states (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_hidden_states=True`` is passed or when ``config.output_hidden_states=True``):
            Tuple of :obj:`tf.Tensor` (one for the output of the embeddings and one for the output of each layer) of
            shape :obj:`(batch_size, sequence_length, hidden_size)`.

            Hidden states of the generator decoder at the output of each layer plus the initial embedding outputs.
        generator_dec_attentions (:obj:`tuple(tf.Tensor)`, `optional`, returned when ``output_attentions=True`` is passed or when ``config.output_attentions=True``):
            Tuple of :obj:`tf.Tensor` (one for each layer) of shape :obj:`(batch_size, num_heads, sequence_length,
            sequence_length)`.

            Attentions weights of the generator decoder, after the attention softmax, used to compute the weighted
            average in the self-attention heads.
    """

    logits: tf.Tensor = None
    doc_scores: tf.Tensor = None
    past_key_values: Optional[List[tf.Tensor]] = None
    retrieved_doc_embeds: Optional[tf.Tensor] = None
    retrieved_doc_ids: Optional[tf.Tensor] = None
    context_input_ids: Optional[tf.Tensor] = None
    context_attention_mask: Optional[tf.Tensor] = None
    question_encoder_last_hidden_state: Optional[tf.Tensor] = None
    question_enc_hidden_states: Optional[Tuple[tf.Tensor]] = None
    question_enc_attentions: Optional[Tuple[tf.Tensor]] = None
    generator_enc_last_hidden_state: Optional[tf.Tensor] = None
    generator_enc_hidden_states: Optional[Tuple[tf.Tensor]] = None
    generator_enc_attentions: Optional[Tuple[tf.Tensor]] = None
    generator_dec_hidden_states: Optional[Tuple[tf.Tensor]] = None
    generator_dec_attentions: Optional[Tuple[tf.Tensor]] = None


class TFRagPreTrainedModel(TFPreTrainedModel):
    r"""
    RAG models were released with the paper `Retrieval-Augmented Generation for Knowledge-Intensive NLP Tasks
    <https://arxiv.org/abs/2005.11401>`_ by Patrick Lewis, Ethan Perez, Aleksandra Piktus et al.

    RAG is a retriever augmented model and encapsulate three components: a question encoder, a dataset retriever and a
    generator, the encoder and generator are trainable while the retriever is just an indexed dataset.

    """
    config_class = RagConfig
    base_model_prefix = "rag"
    _keys_to_ignore_on_load_missing = [r"position_ids"]

    @classmethod
    def from_pretrained_question_encoder_generator(
        cls,
        question_encoder_pretrained_model_name_or_path: str = None,
        generator_pretrained_model_name_or_path: str = None,
        retriever: RagRetriever = None,
        *model_args,
        **kwargs
    ) -> TFPreTrainedModel:
        r"""
        Instantiates an question encoder and a generator from one or two base classes of the library from pretrained
        model checkpoints.

        Params:
            question_encoder_pretrained_model_name_or_path (:obj: `str`, `optional`, defaults to `None`):
                Information necessary to initiate the question encoder. Can be either:

                    - A string with the `shortcut name` of a pretrained model to load from cache or download, e.g.,
                      ``bert-base-uncased``.
                    - A string with the `identifier name` of a pretrained model that was user-uploaded to our S3, e.g.,
                      ``dbmdz/bert-base-german-cased``.
                    - A path to a `directory` containing model weights saved using
                      :func:`~transformers.TFPreTrainedModel.save_pretrained`, e.g., ``./my_model_directory/``.
                    - A path or url to a `pytorch index checkpoint file` (e.g, ``./pt_model/``). In this case,
                      ``question_encoder_from_pt`` should be set to :obj:`True`.

            generator_pretrained_model_name_or_path (:obj: `str`, `optional`, defaults to `None`):
                Information necessary to initiate the generator. Can be either:

                    - A string with the `shortcut name` of a pretrained model to load from cache or download, e.g.,
                      ``t5-small``.
                    - A string with the `identifier name` of a pretrained model that was user-uploaded to our S3, e.g.,
                      ``facebook/bart-base``.
                    - A path to a `directory` containing model weights saved using
                      :func:`~transformers.TFPreTrainedModel.save_pretrained`, e.g., ``./my_model_directory/``.
                    - A path or url to a `pytorch checkpoint file` (e.g, ``./pt_model/``). In this case,
                      ``generator_from_pt`` should be set to :obj:`True`.

            model_args (remaining positional arguments, `optional`):
                All remaning positional arguments will be passed to the underlying model's ``__init__`` method.
            retriever (:class:`~transformers.RagRetriever`, `optional`):
                The retriever to use.
            kwargs (remaining dictionary of keyword arguments, `optional`):
                Can be used to update the configuration object (after it being loaded) and initiate the model (e.g.,
                ``output_attentions=True``).

                - To update the question_encoder configuration, use the prefix `question_encoder_` for each
                  configuration parameter.
                - To update the generator configuration, use the prefix `generator_` for each configuration parameter.
                - To update the parent model configuration, do not use a prefix for each configuration parameter.

                Behaves differently depending on whether a :obj:`config` is provided or automatically loaded.

        Example::

            >>> from transformers import RagRetriever, TFRagModel
            >>> # initialize a RAG from two pretrained models.
            >>> model = TFRagModel.from_pretrained_question_encoder_generator('facebook/dpr-question_encoder-single-nq-base', 't5-small')
            >>> # alternatively, initialize from pytorch pretrained models can also be done
            >>> model = TFRagModel.from_pretrained_question_encoder_generator('facebook/dpr-question_encoder-single-nq-base', "facebook/bart-base", generator_from_pt=True, question_encoder_from_pt=True)

            >>> # saving model after fine-tuning
            >>> model.save_pretrained("./rag")

            >>> # load retriever
            >>> retriever = RagRetriever.from_pretrained(PATH, index_name="exact", use_dummy_dataset=True)
            >>> # load fine-tuned model with retriver
            >>> model = TFRagModel.from_pretrained("./rag", retriever=retriever)
        """

        kwargs_question_encoder = {
            argument[len("question_encoder_") :]: value
            for argument, value in kwargs.items()
            if argument.startswith("question_encoder_")
        }

        kwargs_generator = {
            argument[len("generator_") :]: value
            for argument, value in kwargs.items()
            if argument.startswith("generator_")
        }

        # remove question_encoder, generator kwargs from kwargs
        for key in kwargs_question_encoder.keys():
            del kwargs["question_encoder_" + key]
        for key in kwargs_generator.keys():
            del kwargs["generator_" + key]

        # Load and initialize the question_encoder and generator
        # The distinction between question_encoder and generator at the model level is made
        # by the value of the flag `is_generator` that we need to set correctly.
        question_encoder = kwargs_question_encoder.pop("model", None)
        if question_encoder is None:
            assert (
                question_encoder_pretrained_model_name_or_path is not None
            ), "If `model` is not defined as an argument, a `question_encoder_pretrained_model_name_or_path` has to be defined"

            from ..auto.modeling_tf_auto import TFAutoModel

            if "config" not in kwargs_question_encoder:
                from ..auto.configuration_auto import AutoConfig

                question_encoder_config = AutoConfig.from_pretrained(question_encoder_pretrained_model_name_or_path)
                kwargs_question_encoder["config"] = question_encoder_config

            question_encoder = TFAutoModel.from_pretrained(
                question_encoder_pretrained_model_name_or_path,
                name="question_encoder",
                load_weight_prefix=cls.load_weight_prefix,
                *model_args,
                **kwargs_question_encoder,
            )

        generator = kwargs_generator.pop("generator", None)
        if generator is None:
            assert (
                generator_pretrained_model_name_or_path is not None
            ), "If `generator_model` is not defined as an argument, a `generator_pretrained_model_name_or_path` has to be defined"

            from ..auto.modeling_tf_auto import TFAutoModelForSeq2SeqLM

            if "config" not in kwargs_generator:
                from ..auto.configuration_auto import AutoConfig

                generator_config = AutoConfig.from_pretrained(generator_pretrained_model_name_or_path)
                kwargs_generator["config"] = generator_config

            generator = TFAutoModelForSeq2SeqLM.from_pretrained(
                generator_pretrained_model_name_or_path,
                name="generator",
                load_weight_prefix=cls.load_weight_prefix,
                **kwargs_generator,
            )

        # instantiate config with corresponding kwargs
        config = kwargs.get("config", None)
        if config is None:
            config = RagConfig.from_question_encoder_generator_configs(
                question_encoder.config, generator.config, **kwargs
            )

        return cls(question_encoder=question_encoder, generator=generator, config=config, retriever=retriever)


RAG_START_DOCSTRING = r"""

    RAG is a seq2seq model which encapsulates two core components: a question encoder and a generator. During a forward
    pass, we encode the input with the question encoder and pass it to the retriever to extract relevant context
    documents. The documents are then prepended to the input. Such contextualized inputs is passed to the generator.

    The question encoder can be any `autoencoding` model, preferably :class:`~transformers.TFDPRQuestionEncoder`, and
    the generator can be any `seq2seq` model, preferably :class:`~transformers.TFBartForConditionalGeneration`.

    The model can be initialized with a :class:`~transformers.RagRetriever` for end-to-end generation or used in
    combination with the outputs of a retriever in multiple steps---see examples for more details. The model is
    compatible any `autoencoding` model as the ``question_encoder`` and any `seq2seq` model with language model head as
    the ``generator``. It has been tested with :class:`~transformers.TFDPRQuestionEncoder` as the ``question_encoder``
    and :class:`~transformers.TFBartForConditionalGeneration` or :class:`~transformers.TFT5ForConditionalGeneration` as
    the ``generator``.

    This model inherits from :class:`~transformers.TFPreTrainedModel`. Check the superclass documentation for the
    generic methods the library implements for all its model (such as downloading or saving, resizing the input
    embeddings, pruning heads etc.)

    This model is also a Tensorflow `tf.keras.Model <https://www.tensorflow.org/api_docs/python/tf/keras/Model>`__
    subclass. Use it as a regular TF 2.0 Keras Model and refer to the TF 2.0 documentation for all matter related to
    general usage and behavior.

    Args:
        config (:class:`~transformers.RagConfig`):
            Model configuration class with all the parameters of the model. Initializing with a config file does not
            load the weights associated with the model, only the configuration. Check out the
            :meth:`~transformers.TFPreTrainedModel.from_pretrained` method to load the model weights.
        question_encoder (:class:`transformers.TFPreTrainedModel`):
            An encoder model compatible with the faiss index encapsulated by the ``retriever``.
        generator (:class:`transformers.TFPreTrainedModel`):
            A seq2seq model used as the generator in the RAG architecture.
        retriever (:class:`~transformers.RagRetriever`):
            A retriever class encapsulating a faiss index queried to obtain context documents for current inputs.
"""


RAG_FORWARD_INPUTS_DOCSTRING = r"""
    Args:
        input_ids (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length)`):
            Indices of input sequence tokens in the vocabulary. :class:`~transformers.RagConfig`, used to initialize
            the model, specifies which generator to use, it also specifies a compatible generator tokenizer. Use that
            tokenizer class to obtain the indices.
        attention_mask (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
            Mask to avoid performing attention on padding token indices. Mask values selected in ``[0, 1]``:

            - 1 for tokens that are **not masked**,
            - 0 for tokens that are **masked**.

            `What are attention masks? <../glossary.html#attention-mask>`__
        encoder_outputs (:obj:`tuple(tuple(tf.Tensor)`, `optional`)
            Tuple consists of (:obj:`generator_enc_last_hidden_state`, `optional`: :obj:`generator_enc_hidden_states`,
            `optional`: :obj:`generator_enc_attentions`). :obj:`generator_enc_last_hidden_state` of shape
            :obj:`(batch_size, n_docs * sequence_length, hidden_size)` is a sequence of hidden-states at the output of
            the last layer of the generator's encoder.

            Used by the (:class:`~transformers.TFRagModel`) model during decoding.
        decoder_input_ids (:obj:`tf.Tensor` of shape :obj:`(batch_size, target_sequence_length)`, `optional`):
            Provide for generation tasks. `None` by default, construct as per instructions for the generator model
            you're using with your RAG instance.
        decoder_attention_mask (:obj:`torch.BoolTensor` of shape :obj:`(batch_size,  target_sequence_length)`, `optional`):
            Default behavior: generate a tensor that ignores pad tokens in :obj:`decoder_input_ids`. Causal mask will
            also be used by default.
        past_key_values (:obj:`tuple(tuple(tf.Tensor))`):
            Tuple consists of two elements: :obj:`encoder_outputs` of the RAG model (see :obj:`encoder_outputs`) and
            :obj:`past_key_values` of the underlying generator. Can be used to speed up decoding.
            :obj:`past_key_values` are used in the (:class:`~transformers.RagTokenForGeneration`) model during
            decoding.
        doc_scores (:obj:`tf.Tensor` of shape :obj:`(batch_size, config.n_docs)`):
            Score between each retrieved document embeddings (see :obj:`retrieved_doc_embeds`) and
            :obj:`question_encoder_last_hidden_state`. If the model has is not initialized with a ``retriever``
            :obj:`doc_scores` has to be provided to the forward pass. :obj:`doc_scores` can be computed via
            :obj:`question_encoder_last_hidden_state` and :obj:`retrieved_doc_embeds`, see examples for more
            information.
        context_input_ids (:obj:`tf.Tensor` of shape :obj:`(batch_size * config.n_docs, config.max_combined_length)`, `optional`, returned when `output_retrieved=True`):
            Input IDs post-processed from the retrieved documents and the question encoder :obj:`input_ids` by the
            retriever.

            If the model has is not initialized with a ``retriever`` :obj:`context_input_ids` has to be provided to the
            forward pass. :obj:`context_input_ids` are returned by :meth:`~transformers.RagRetriever.__call__`.
        context_attention_mask (:obj:`tf.Tensor` of shape :obj:`(batch_size * config.n_docs, config.max_combined_length)`, `optional`, returned when `output_retrieved=True`):
            Attention mask post-processed from the retrieved documents and the question encoder :obj:`input_ids` by the
            retriever.

            If the model has is not initialized with a ``retriever`` :obj:`context_attention_mask` has to be provided
            to the forward pass. :obj:`context_attention_mask` are returned by
            :meth:`~transformers.RagRetriever.__call__`.
        use_cache (:obj:`bool`, `optional`, defaults to :obj:`True`):
            If set to :obj:`True`, :obj:`past_key_values` key value states are returned and can be used to speed up
            decoding (see :obj:`past_key_values`).
        output_attentions (:obj:`bool`, `optional`):
            Whether or not to return the attentions tensors of all attention layers. See ``attentions`` under returned
            tensors for more detail.
        output_hidden_states (:obj:`bool`, `optional`):
            Whether or not to return the hidden states of all layers. See ``hidden_states`` under returned tensors for
            more detail.
        output_retrieved(:obj:`bool`, `optional`):
            Whether or not to return the :obj:`retrieved_doc_embeds`, :obj:`retrieved_doc_ids`,
            :obj:`context_input_ids` and :obj:`context_attention_mask`. See returned tensors for more detail.
        n_docs (:obj:`int`, `optional`, defaults to :obj:`config.n_docs`)
            Number of documents to retrieve and/or number of documents for which to generate an answer.
"""


@add_start_docstrings_to_model_forward(RAG_START_DOCSTRING)
class TFRagModel(TFRagPreTrainedModel):

    load_weight_prefix = "tf_rag_model_1"

    def __init__(
        self,
        config: Optional[PretrainedConfig] = None,
        question_encoder: Optional[TFPreTrainedModel] = None,
        generator: Optional[TFPreTrainedModel] = None,
        retriever: Optional = None,
        load_weight_prefix: Optional[str] = None,
        **kwargs,
    ):
        assert config is not None or (
            question_encoder is not None and generator is not None
        ), "Either a configuration or an question_encoder and a generator has to be provided."

        if config is None:
            config = RagConfig.from_question_encoder_generator_configs(
                question_encoder.config, generator.config, **kwargs
            )
        else:
            assert isinstance(config, self.config_class), "config: {} has to be of type {}".format(
                config, self.config_class
            )
        super().__init__(config, **kwargs)

        if question_encoder is None:
            from ..auto.modeling_tf_auto import TFAutoModel

            question_encoder = TFAutoModel.from_config(config.question_encoder, name="question_encoder")

        if generator is None:
            from ..auto.modeling_tf_auto import TFAutoModelForSeq2SeqLM

            load_weight_prefix = load_weight_prefix if load_weight_prefix is not None else self.load_weight_prefix
            generator = TFAutoModelForSeq2SeqLM.from_config(
                config.generator, name="generator", load_weight_prefix=load_weight_prefix + "/generator"
            )

        self.retriever = retriever
        if self.retriever is not None:
            assert isinstance(
                retriever, RagRetriever
            ), f"`self.retriever` is of type {type(self.retriever)}, but should be of type `RagRetriever`"
            self.retriever = retriever

        self.question_encoder = question_encoder
        self.generator = generator

    def set_retriever(self, retriever: RagRetriever):
        self.retriever = retriever

    @add_start_docstrings_to_model_forward(RAG_FORWARD_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=TFRetrievAugLMOutput, config_class=_CONFIG_FOR_DOC)
    def call(
        self,
        input_ids=None,
        attention_mask=None,
        encoder_outputs=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        past_key_values=None,
        doc_scores=None,
        context_input_ids=None,
        context_attention_mask=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        output_retrieved=None,
        n_docs=None,
        return_dict=None,
        training=False,
        **kwargs
    ):
        r"""
        Returns:

        Example::

            >>> from transformers import RagTokenizer, RagRetriever, RagModel
            >>> import torch

            >>> tokenizer = RagTokenizer.from_pretrained("facebook/rag-token-base")
            >>> retriever = RagRetriever.from_pretrained("facebook/rag-token-base", index_name="exact", use_dummy_dataset=True)
            >>> # initialize with RagRetriever to do everything in one forward call
            >>> model = TFRagModel.from_pretrained("facebook/rag-token-base", retriever=retriever, from_pt=True)

            >>> input_dict = tokenizer.prepare_seq2seq_batch("How many people live in Paris?", "In Paris, there are 10 million people.", return_tensors="tf")
            >>> input_ids = input_dict["input_ids"]
            >>> outputs = model(input_ids)

        """
        assert (
            "decoder_cached_states" not in kwargs
        ), "Please use past_key_values to cache intermediate outputs"  # from modeling_tf_bart.py

        inputs = input_processing(
            func=self.call,
            config=self.config,
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            encoder_outputs=encoder_outputs,
            past_key_values=past_key_values,
            doc_scores=doc_scores,
            context_input_ids=context_input_ids,
            context_attention_mask=context_attention_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            output_retrieved=output_retrieved,
            return_dict=return_dict,
            n_docs=n_docs,
            training=training,
            kwargs_call=kwargs,
        )

        # aliasing to minimize code changing
        input_ids = inputs["input_ids"]
        attention_mask = inputs["attention_mask"]
        decoder_input_ids = inputs["decoder_input_ids"]
        decoder_attention_mask = inputs["decoder_attention_mask"]
        encoder_outputs = inputs["encoder_outputs"]
        past_key_values = inputs["past_key_values"]
        doc_scores = inputs["doc_scores"]
        context_input_ids = inputs["context_input_ids"]
        context_attention_mask = inputs["context_attention_mask"]

        use_cache = inputs["use_cache"]
        output_attentions = inputs["output_attentions"]
        output_hidden_states = inputs["output_hidden_states"]
        return_dict = inputs["return_dict"]
        n_docs = inputs["n_docs"] if inputs["n_docs"] is not None else self.config.n_docs
        output_retrieved = inputs["output_retrieved"]
        training = inputs["training"]

        # whether retriever has to be used
        has_to_retrieve = (
            self.retriever is not None
            and (context_input_ids is None or context_attention_mask is None or doc_scores is None)
            and encoder_outputs is None
        )

        # encoder_outputs are pre-computed during RAG-token generation
        if encoder_outputs is None:

            if has_to_retrieve:
                question_enc_outputs = self.question_encoder(
                    input_ids, attention_mask=attention_mask, return_dict=True, training=training
                )
                # see https://github.com/huggingface/transformers/blob/master/src/transformers/models/dpr/modeling_tf_dpr.py#L91
                question_encoder_last_hidden_state = question_enc_outputs[
                    0
                ]  # hidden states of question encoder => pooler_output

                retriever_outputs = self.retriever(
                    input_ids,
                    question_encoder_last_hidden_state.numpy(),  # NEED_HELP : not work in GRAPH mode, tf.make_ndarray doesn't work as well
                    prefix=self.generator.config.prefix,
                    n_docs=n_docs,
                    return_tensors="tf",
                )
                context_input_ids, context_attention_mask, retrieved_doc_embeds, retrieved_doc_ids = (
                    retriever_outputs["context_input_ids"],
                    retriever_outputs["context_attention_mask"],
                    retriever_outputs["retrieved_doc_embeds"],
                    retriever_outputs["doc_ids"],
                )

                # compute doc_scores
                doc_scores = tf.squeeze(
                    tf.matmul(
                        tf.expand_dims(question_encoder_last_hidden_state, axis=[1]),
                        retrieved_doc_embeds,
                        transpose_b=True,
                    ),
                    axis=[1],
                )

            else:
                assert (
                    context_input_ids is not None
                ), "Make sure that `context_input_ids` are passed, if no `retriever` is set. Alternatively, you can set a retriever using the `set_retriever(...)` function."
                assert (
                    context_attention_mask is not None
                ), "Make sure that `context_attention_mask` are passed, if no `retriever` is set. Alternatively, you can set a retriever using the `set_retriever(...)` function."
                assert (
                    doc_scores is not None
                ), "Make sure that `doc_scores` are passed, if no `retriever` is set. Alternatively, you can set a retriever using the `set_retriever(...)` function."

        assert (
            doc_scores is not None
        ), "Make sure that `doc_scores` are passed when passing `encoder_outputs` to the forward function."

        assert (
            doc_scores.shape[1] % n_docs
        ) == 0, f" The first dimension of `context_input_ids` should be a multiple of `n_docs`={n_docs}, but is {context_input_ids.shape[0]}."

        # Decoder input without context documents
        if decoder_input_ids is not None:
            decoder_input_ids = tf.repeat(decoder_input_ids, n_docs, axis=0)

        if decoder_attention_mask is not None:
            decoder_attention_mask = tf.repeat(decoder_attention_mask, n_docs, axis=0)

        gen_outputs = self.generator(
            context_input_ids,
            attention_mask=context_attention_mask,
            encoder_outputs=encoder_outputs,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            past_key_values=past_key_values,
            use_cache=use_cache,
            return_dict=True,
            training=training,
        )

        if not has_to_retrieve:
            question_encoder_last_hidden_state = None
            question_enc_hidden_states = None
            question_enc_attentions = None
            retrieved_doc_embeds = None
            retrieved_doc_ids = None
        else:
            question_enc_hidden_states = question_enc_outputs.hidden_states
            question_enc_attentions = question_enc_outputs.attentions

        if not has_to_retrieve or not output_retrieved:
            # don't output retrieved docs
            context_input_ids = (None,)
            context_attention_mask = None
            retrieved_doc_embeds = None
            retrieved_doc_ids = None

        return TFRetrievAugLMOutput(
            logits=gen_outputs.logits,
            doc_scores=doc_scores,
            past_key_values=gen_outputs.past_key_values,
            context_input_ids=context_input_ids,
            context_attention_mask=context_attention_mask,
            retrieved_doc_embeds=retrieved_doc_embeds,
            retrieved_doc_ids=retrieved_doc_ids,
            question_encoder_last_hidden_state=question_encoder_last_hidden_state,
            question_enc_hidden_states=question_enc_hidden_states,
            question_enc_attentions=question_enc_attentions,
            generator_enc_last_hidden_state=gen_outputs.encoder_last_hidden_state,
            generator_enc_hidden_states=gen_outputs.encoder_hidden_states,
            generator_enc_attentions=gen_outputs.encoder_attentions,
            generator_dec_hidden_states=gen_outputs.decoder_hidden_states,
            generator_dec_attentions=gen_outputs.decoder_attentions,
        )


@add_start_docstrings_to_model_forward(
    """
    A TF RAG-token model implementation. It performs RAG-token specific marginalization in the forward pass.
    """,
    RAG_START_DOCSTRING,
)
class TFRagTokenForGeneration(TFRagPreTrainedModel, TFCausalLanguageModelingLoss):

    load_weight_prefix = "tf_rag_token_for_generation_1/rag"

    def __init__(
        self,
        config: Optional[PretrainedConfig] = None,
        question_encoder: Optional[TFPreTrainedModel] = None,
        generator: Optional[TFPreTrainedModel] = None,
        retriever: Optional = None,
        **kwargs,
    ):
        assert config is not None or (
            question_encoder is not None and generator is not None
        ), "Either a configuration or an encoder and a generator has to be provided."

        if config is None:
            config = RagConfig.from_question_encoder_generator_configs(
                question_encoder.config, generator.config, **kwargs
            )

        super().__init__(config)

        # instantiate model
        self.rag = TFRagModel(
            config=config,
            question_encoder=question_encoder,
            generator=generator,
            retriever=retriever,
            load_weight_prefix=self.load_weight_prefix,
            name="rag",
        )

    def set_retriever(self, retriever: RagRetriever):
        self.rag.retriever = retriever

    def adjust_logits_during_generation(self, logits, cur_len, max_length):
        return self.rag.generator.adjust_logits_during_generation(logits, cur_len=cur_len, max_length=max_length)

    # Adapted from https://github.com/huggingface/transformers/blob/master/src/transformers/modeling_tf_bart.py
    def prepare_inputs_for_generation(
        self, decoder_input_ids, past, attention_mask, use_cache, encoder_outputs, doc_scores, n_docs=None, **kwargs
    ) -> Dict:
        assert past is not None and len(past) in {1, 2}, f"past has to be an iterable of length 1,2 got {past}"

        if len(past) == 1:
            assert isinstance(past[0], tf.Tensor)
            encoder_outputs = TFBaseModelOutput(last_hidden_state=past[0])
            decoder_cached_states = None
        else:
            assert len(past) == 2
            # Note: encoder_outputs is never changed by Bart as a generator
            encoder_outputs, decoder_cached_states = past

            if isinstance(encoder_outputs, tuple):
                assert isinstance(encoder_outputs[0], tf.Tensor)
                encoder_outputs = TFBaseModelOutput(last_hidden_state=encoder_outputs[0])
            elif isinstance(encoder_outputs, tf.Tensor):
                encoder_outputs = TFBaseModelOutput(last_hidden_state=encoder_outputs)

            assert (
                decoder_cached_states
            ), f"decoder cached states must be truthy. got {decoder_cached_states} from the 2nd element of past"
            # if past is defined cut decoder_input_ids to last token
            decoder_input_ids = decoder_input_ids[:, -1:]

        assert isinstance(
            encoder_outputs, TFBaseModelOutput
        ), f"encoder_outputs should be a TFBaseModelOutput, Instead got {type(encoder_outputs)}."
        return {
            "input_ids": None,  # encoder_outputs is defined. input_ids not needed
            "encoder_outputs": encoder_outputs,
            "doc_scores": doc_scores,
            "context_attention_mask": attention_mask,
            "decoder_input_ids": decoder_input_ids,
            "past_key_values": decoder_cached_states,  # This is due to TFBart and is the main difference to Pytorch's RAG
            "use_cache": use_cache,  # change this to avoid caching (presumably for debugging)
            "do_marginalize": True,
            "n_docs": n_docs,
        }

    @property
    def retriever(self):
        return self.rag.retriever

    @property
    def generator(self):
        return self.rag.generator

    @property
    def question_encoder(self):
        return self.rag.question_encoder

    @staticmethod
    def _reorder_cache(past, beam_idx):
        """Reorders cache for generation. BART-inspired but we need to take care of the extra dimension for docs"""
        
        def tf_index_select(input_, dim, indices):
            """
            Input:
                input_(tensor): input tensor
                dim(int): dimension
                indices(list): selected indices list
            Output:
                mimic of torch_tensor.index_select(dim, indices)
            
            credit: https://stackoverflow.com/questions/58464790/is-there-an-equivalent-function-of-pytorch-named-index-select-in-tensorflow
            """
            shape = input_.get_shape().as_list()
            if dim == -1:
                dim = len(shape)-1
            shape[dim] = 1

            tmp = []
            for idx in indices:
                begin = [0]*len(shape)
                begin[dim] = idx
                tmp.append(tf.slice(input_, begin, shape))
            res = tf.concat(tmp, axis=dim)

            return res

        def _reorder_stacked(hidden_states, new_order=beam_idx):
            n_docs = hidden_states.shape[0] // new_order.shape[0]
            hidden_states = tf.reshape(hidden_states, (-1, n_docs, *hidden_states.shape[1:]) )
            hidden_states = tf_index_select(hidden_states, 0, new_order)
            return tf.reshape(hidden_states, (-1, *hidden_states.shape[2:]) )

        if len(past)==1:
            return past

        past_key_values = past[1]

        reordered_past = ()
        for layer_past in past_key_values:
            reordered_past += (tuple(_reorder_stacked(past_state, beam_idx) for past_state in layer_past),)

        return (past[0], reordered_past)

    def marginalize(self, seq_logits, doc_scores, n_docs=None):
        n_docs = n_docs if n_docs is not None else self.config.n_docs

        # RAG-token marginalization
        seq_logprobs = tf.nn.log_softmax(seq_logits, axis=-1)
        seq_logprobs = tf.reshape(seq_logprobs, [seq_logits.shape[0] // n_docs, n_docs, -1, seq_logits.shape[-1]])
        doc_logprobs = tf.nn.log_softmax(doc_scores, axis=1)
        doc_logprobs = tf.expand_dims(doc_logprobs, axis=-1)
        doc_logprobs = tf.expand_dims(doc_logprobs, axis=-1)  # twice
        log_prob_sum = seq_logprobs + doc_logprobs
        return tf.reduce_logsumexp(log_prob_sum, axis=1)

    @add_start_docstrings_to_model_forward(RAG_FORWARD_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=TFRetrievAugLMMarginOutput, config_class=_CONFIG_FOR_DOC)
    def call(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        encoder_outputs=None,
        past_key_values=None,
        doc_scores=None,
        context_input_ids=None,
        context_attention_mask=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        output_retrieved=None,
        n_docs=None,
        do_marginalize=None,
        labels=None,
        reduce_loss=None,
        training=False,
        **kwargs  # needs kwargs for generation
    ):
        r"""
        do_marginalize (:obj:`bool`, `optional`):
            If :obj:`True`, the logits are marginalized over all documents by making use of
            ``torch.nn.functional.log_softmax``.
        reduce_loss (:obj:`bool`, `optional`):
            Only relevant if ``labels`` is passed. If :obj:`True`, the NLL loss is reduced using the ``tf.Tensor.sum``
            operation.
        kwargs (:obj:`Dict[str, any]`, optional, defaults to `{}`):
            Legacy dictionary, which is required so that model can use `generate()` function.

        Returns:

        Example::

            >>> from transformers import RagTokenizer, RagRetriever, TFRagTokenForGeneration

            >>> tokenizer = RagTokenizer.from_pretrained("facebook/rag-token-nq")
            >>> retriever = RagRetriever.from_pretrained("facebook/rag-token-nq", index_name="exact", use_dummy_dataset=True)
            >>> # initialize with RagRetriever to do everything in one forward call
            >>> model = TFRagTokenForGeneration.from_pretrained("facebook/rag-token-nq", retriever=retriever, from_pt=True)

            >>> input_dict = tokenizer.prepare_seq2seq_batch("How many people live in Paris?", "In Paris, there are 10 million people.", return_tensors="tf")
            >>> outputs = model(input_dict, output_retrieved=True)

            >>> # or use retriever separately
            >>> # 1. Encode
            >>> input_ids = input_dict["input_ids"]
            >>> question_hidden_states = model.question_encoder(input_ids)[0]
            >>> # 2. Retrieve
            >>> docs_dict = retriever(input_ids.numpy(), question_hidden_states.numpy(), return_tensors="tf")
            >>> doc_scores = tf.squeeze(tf.matmul(tf.expand_dims(question_hidden_states, axis=[1]), docs_dict["retrieved_doc_embeds"], transpose_b=True), axis=[1])
            >>> # 3. Forward to generator
            >>> outputs = model(inputs=None, context_input_ids=docs_dict["context_input_ids"], context_attention_mask=docs_dict["context_attention_mask"], doc_scores=doc_scores, decoder_input_ids=input_dict["labels"])

            >>> # or directly generate
            >>> generated = model.generate(context_input_ids=docs_dict["context_input_ids"], context_attention_mask=docs_dict["context_attention_mask"], doc_scores=doc_scores)
            >>> generated_string = tokenizer.batch_decode(generated, skip_special_tokens=True)
        """

        assert (
            "decoder_cached_states" not in kwargs
        ), "Please use past_key_values to cache intermediate outputs"  # from modeling_tf_bart.py

        inputs = input_processing(
            func=self.call,
            config=self.config,
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            encoder_outputs=encoder_outputs,
            past_key_values=past_key_values,
            doc_scores=doc_scores,
            context_input_ids=context_input_ids,
            context_attention_mask=context_attention_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            output_retrieved=output_retrieved,
            n_docs=n_docs,
            do_marginalize=do_marginalize,
            labels=labels,
            reduce_loss=reduce_loss,
            training=training,
            kwargs_call=kwargs,
        )

        inputs["do_marginalize"] = inputs["do_marginalize"] if inputs["do_marginalize"] else self.config.do_marginalize
        inputs["reduce_loss"] = inputs["reduce_loss"] if inputs["reduce_loss"] else self.config.reduce_loss

        if inputs["labels"] is not None:
            if inputs["decoder_input_ids"] is None:
                inputs["decoder_input_ids"] = inputs["labels"]
            inputs["use_cache"] = False

        outputs = self.rag(
            inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            encoder_outputs=inputs["encoder_outputs"],
            decoder_input_ids=inputs["decoder_input_ids"],
            decoder_attention_mask=inputs["decoder_attention_mask"],
            context_input_ids=inputs["context_input_ids"],
            context_attention_mask=inputs["context_attention_mask"],
            doc_scores=inputs["doc_scores"],
            past_key_values=inputs["past_key_values"],
            use_cache=inputs["use_cache"],
            output_attentions=inputs["output_attentions"],
            output_hidden_states=inputs["output_hidden_states"],
            output_retrieved=inputs["output_retrieved"],
            n_docs=inputs["n_docs"],
            training=inputs["training"],
        )

        loss = None
        logits = outputs.logits
        if inputs["labels"] is not None:
            assert inputs["decoder_input_ids"] is not None
            loss = self.get_nll(
                outputs.logits,
                outputs.doc_scores,
                inputs["labels"],
                reduce_loss=inputs["reduce_loss"],
                epsilon=self.config.label_smoothing,
                n_docs=inputs["n_docs"],
            )

        if inputs["do_marginalize"]:
            logits = self.marginalize(logits, outputs.doc_scores, inputs["n_docs"])

        return TFRetrievAugLMMarginOutput(
            loss=loss,
            logits=logits,
            doc_scores=outputs.doc_scores,
            past_key_values=outputs.past_key_values,
            context_input_ids=outputs.context_input_ids,
            context_attention_mask=outputs.context_attention_mask,
            retrieved_doc_embeds=outputs.retrieved_doc_embeds,
            retrieved_doc_ids=outputs.retrieved_doc_ids,
            question_encoder_last_hidden_state=outputs.question_encoder_last_hidden_state,
            question_enc_hidden_states=outputs.question_enc_hidden_states,
            question_enc_attentions=outputs.question_enc_attentions,
            generator_enc_last_hidden_state=outputs.generator_enc_last_hidden_state,
            generator_enc_hidden_states=outputs.generator_enc_hidden_states,
            generator_enc_attentions=outputs.generator_enc_attentions,
            generator_dec_hidden_states=outputs.generator_dec_hidden_states,
            generator_dec_attentions=outputs.generator_dec_attentions,
        )

    def generate(
        self,
        input_ids: Optional[tf.Tensor] = None,
        attention_mask: Optional[tf.Tensor] = None,
        context_input_ids=None,
        context_attention_mask=None,
        doc_scores=None,
        max_length=None,
        min_length=None,
        early_stopping=None,
        use_cache=None,
        num_beams=None,
        bos_token_id=None,
        pad_token_id=None,
        eos_token_id=None,
        length_penalty=None,
        no_repeat_ngram_size=None,
        bad_words_ids=None,
        num_return_sequences=None,
        decoder_start_token_id=None,
        n_docs=None,
        **kwargs
    ):
        """
        Implements TFRAG token decoding.

        Args:
            input_ids (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
                The sequence used as a prompt for the generation. If :obj:`input_ids` is not passed, then
                :obj:`context_input_ids` has to be provided.
            attention_mask (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
                Mask to avoid performing attention on padding token indices. Mask values selected in ``[0, 1]``:

                - 1 for tokens that are **not masked**,
                - 0 for tokens that are **masked**.

                `What are attention masks? <../glossary.html#attention-mask>`__
            context_input_ids (:obj:`tf.Tensor` of shape :obj:`(batch_size * config.n_docs, config.max_combined_length)`, `optional`, returned when `output_retrieved=True`):
                Input IDs post-processed from the retrieved documents and the question encoder :obj:`input_ids` by the
                retriever.

                If the model has is not initialized with a ``retriever``, :obj:`context_input_ids` has to be provided
                to the forward pass. :obj:`context_input_ids` are returned by
                :meth:`~transformers.RagRetriever.__call__`.
            context_attention_mask (:obj:`tf.Tensor` of shape :obj:`(batch_size * config.n_docs, config.max_combined_length)`, `optional`, returned when `output_retrieved=True`):
                Attention mask post-processed from the retrieved documents and the question encoder :obj:`input_ids` by
                the retriever.

                If the model has is not initialized with a ``retriever``, :obj:`context_input_ids` has to be provided
                to the forward pass. :obj:`context_input_ids` are returned by
                :meth:`~transformers.RagRetriever.__call__`.
            doc_scores (:obj:`tf.Tensor` of shape :obj:`(batch_size, config.n_docs)`):
                Score between each retrieved document embeddings (see :obj:`retrieved_doc_embeds`) and
                :obj:`question_encoder_last_hidden_state`.

                If the model has is not initialized with a ``retriever``, :obj:`context_input_ids` has to be provided
                to the forward pass. :obj:`context_input_ids` are returned by
                :meth:`~transformers.RagRetriever.__call__`.
            max_length (:obj:`int`, `optional`, defaults to 20):
                The maximum length of the sequence to be generated.
            min_length (:obj:`int`, `optional`, defaults to 10):
                The minimum length of the sequence to be generated.
            early_stopping (:obj:`bool`, `optional`, defaults to :obj:`False`):
                Whether or not to stop the beam search when at least ``num_beams`` sentences are finished per batch or
                not.
            use_cache: (:obj:`bool`, `optional`, defaults to :obj:`True`):
                Whether or not the model should use the past last key/values attentions (if applicable to the model) to
                speed up decoding.
            pad_token_id (:obj:`int`, `optional`):
                The id of the `padding` token.
            bos_token_id (:obj:`int`, `optional`):
                The id of the `beginning-of-sequence` token.
            eos_token_id (:obj:`int`, `optional`):
                The id of the `end-of-sequence` token.
            length_penalty (:obj:`float`, `optional`, defaults to 1.0):
                Exponential penalty to the length. 1.0 means no penalty.

                Set to values < 1.0 in order to encourage the model to generate shorter sequences, to a value > 1.0 in
                order to encourage the model to produce longer sequences.
            no_repeat_ngram_size (:obj:`int`, `optional`, defaults to 0):
                If set to int > 0, all ngrams of that size can only occur once.
            bad_words_ids(:obj:`List[int]`, `optional`):
                List of token ids that are not allowed to be generated. In order to get the tokens of the words that
                should not appear in the generated text, use :obj:`tokenizer.encode(bad_word, add_prefix_space=True)`.
            num_beams (:obj:`int`, `optional`, defaults to 1):
                Number of beams for beam search. 1 means no beam search.
            num_return_sequences(:obj:`int`, `optional`, defaults to 1):
                The number of independently computed returned sequences for each element in the batch. Note that this
                is not the value we pass to the ``generator``'s `:func:`~transformers.PreTrainedModel.generate`
                function, where we set ``num_return_sequences`` to :obj:`num_beams`.
            decoder_start_token_id (:obj:`int`, `optional`):
                If an encoder-decoder model starts decoding with a different token than `bos`, the id of that token.
            n_docs (:obj:`int`, `optional`, defaults to :obj:`config.n_docs`)
                Number of documents to retrieve and/or number of documents for which to generate an answer.

        Return:
            :obj:`tf.Tensor` of shape :obj:`(batch_size * num_return_sequences, sequence_length)`: The generated
            sequences. The second dimension (sequence_length) is either equal to :obj:`max_length` or shorter if all
            batches finished early due to the :obj:`eos_token_id`.
        """
        # set default parameters
        n_docs = n_docs if n_docs is not None else self.config.n_docs
        max_length = max_length if max_length is not None else self.config.max_length
        min_length = min_length if min_length is not None else self.config.min_length
        early_stopping = early_stopping if early_stopping is not None else self.config.early_stopping
        use_cache = use_cache if use_cache is not None else self.config.use_cache
        num_beams = num_beams if num_beams is not None else self.config.num_beams
        bos_token_id = bos_token_id if bos_token_id is not None else self.config.generator.bos_token_id
        pad_token_id = pad_token_id if pad_token_id is not None else self.config.generator.pad_token_id
        eos_token_id = eos_token_id if eos_token_id is not None else self.config.generator.eos_token_id
        length_penalty = length_penalty if length_penalty is not None else self.config.length_penalty
        no_repeat_ngram_size = (
            no_repeat_ngram_size if no_repeat_ngram_size is not None else self.config.no_repeat_ngram_size
        )
        bad_words_ids = bad_words_ids if bad_words_ids is not None else self.config.bad_words_ids
        num_return_sequences = (
            num_return_sequences if num_return_sequences is not None else self.config.num_return_sequences
        )
        decoder_start_token_id = (
            decoder_start_token_id
            if decoder_start_token_id is not None
            else self.config.generator.decoder_start_token_id
        )

        # retrieve docs
        if self.retriever is not None and context_input_ids is None:
            question_hidden_states = self.question_encoder(input_ids, attention_mask=attention_mask)[0]
            out = self.retriever(
                input_ids,
                question_hidden_states.numpy().astype(np.float32),
                prefix=self.generator.config.prefix,
                n_docs=n_docs,
                return_tensors="tf",
            )
            context_input_ids, context_attention_mask, retrieved_doc_embeds = (
                out["context_input_ids"],
                out["context_attention_mask"],
                out["retrieved_doc_embeds"],
            )

            # compute doc_scores
            doc_scores = tf.matmul(
                tf.expand_dims(question_hidden_states, axis=[1]), retrieved_doc_embeds, transpose_b=True
            )
            doc_scores = tf.squeeze(doc_scores, axis=[1])

        assert (
            context_input_ids.shape[0] % n_docs
        ) == 0, f" The first dimension of `context_input_ids` should be a multiple of `n_docs`={n_docs}, but is {context_input_ids.shape[0]}."

        batch_size = context_input_ids.shape[0] // n_docs

        encoder = self.rag.generator.get_encoder()
        encoder_outputs = encoder(input_ids=context_input_ids, attention_mask=context_attention_mask, return_dict=True)

        decoder_input_ids = tf.fill(
            (batch_size * num_beams, 1),
            tf.cast(decoder_start_token_id, tf.int32),
        )
        last_hidden_state = encoder_outputs["last_hidden_state"]

        def extend_enc_output(tensor, num_beams=None):
            """
            Broadcast tensor with `num_beams` replica, with correct order Input: tensor of shape (batch_size*n_docs ,
            d) Output: tensor of shape (batch_size*num_beams*n_docs , d)
            """

            # expand batch_size & num_beam dimensions
            d_shape_list = tensor.shape[1:]

            # split n_docs dimensions
            new_shape = (batch_size, 1, n_docs) + d_shape_list
            tensor = tf.reshape(tensor, new_shape)

            # repeat same last hidden states over `num_beams` dimension
            new_shape = (batch_size, num_beams, n_docs) + d_shape_list
            tensor = tf.broadcast_to(tensor, new_shape)

            # merge `batch_size`, `num_beams`, `num_docs` dims again
            new_shape = (batch_size * num_beams * n_docs,) + d_shape_list
            return tf.reshape(tensor, new_shape)

        # correctly extend last_hidden_state and attention mask
        context_attention_mask = extend_enc_output(context_attention_mask, num_beams=num_beams)
        encoder_outputs["last_hidden_state"] = extend_enc_output(last_hidden_state, num_beams=num_beams)

        doc_scores = tf.repeat(doc_scores, num_beams, axis=0)

        # define start_len & additional parameters
        cur_len = 1
        vocab_size = self.config.generator.vocab_size
        kwargs["doc_scores"] = doc_scores
        kwargs["encoder_outputs"] = encoder_outputs
        kwargs["n_docs"] = n_docs

        # not needed. TODO(PVP): change after generate refactor
        do_sample = False
        temperature = self.config.temperature
        top_k = self.config.top_k
        top_p = self.config.top_p
        repetition_penalty = self.config.repetition_penalty

        if num_beams > 1:
            return self._generate_beam_search(
                decoder_input_ids,
                cur_len=cur_len,
                max_length=max_length,
                min_length=min_length,
                do_sample=do_sample,
                early_stopping=early_stopping,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                no_repeat_ngram_size=no_repeat_ngram_size,
                bad_words_ids=bad_words_ids,
                pad_token_id=pad_token_id,
                eos_token_id=eos_token_id,
                batch_size=batch_size,
                num_return_sequences=num_return_sequences,
                length_penalty=length_penalty,
                num_beams=num_beams,
                vocab_size=vocab_size,
                attention_mask=context_attention_mask,
                use_cache=use_cache,
                **kwargs,  # encoder_outputs is here as in Pytorch's version
            )
        else:
            return self._generate_no_beam_search(
                decoder_input_ids,
                cur_len=cur_len,
                max_length=max_length,
                min_length=min_length,
                do_sample=do_sample,
                temperature=temperature,
                top_k=top_k,
                top_p=top_p,
                repetition_penalty=repetition_penalty,
                no_repeat_ngram_size=no_repeat_ngram_size,
                bad_words_ids=bad_words_ids,
                pad_token_id=pad_token_id,
                eos_token_id=eos_token_id,
                batch_size=batch_size,
                vocab_size=vocab_size,
                attention_mask=context_attention_mask,
                use_cache=use_cache,
                **kwargs,  # encoder_outputs is here as in Pytorch's version
            )

    def _generate_beam_search(
        self,
        input_ids,
        cur_len,
        max_length,
        min_length,
        do_sample,
        early_stopping,
        temperature,
        top_k,
        top_p,
        repetition_penalty,
        no_repeat_ngram_size,
        bad_words_ids,
        pad_token_id,
        eos_token_id,
        batch_size,
        num_return_sequences,
        length_penalty,
        num_beams,
        vocab_size,
        encoder_outputs,
        attention_mask,
        use_cache,
        **kwargs,
    ):
        """Generate sequences for each example with beam search."""

        # generated hypotheses
        generated_hyps = [
            BeamHypotheses(num_beams, max_length, length_penalty, early_stopping=early_stopping)
            for _ in range(batch_size)
        ]

        # for greedy decoding it is made sure that only tokens of the first beam are considered to avoid sampling the exact same tokens three times
        if do_sample is False:
            beam_scores_begin = tf.zeros((batch_size, 1), dtype=tf.float32)
            beam_scores_end = tf.ones((batch_size, num_beams - 1), dtype=tf.float32) * (-1e9)
            beam_scores = tf.concat([beam_scores_begin, beam_scores_end], -1)
        else:
            beam_scores = tf.zeros((batch_size, num_beams), dtype=tf.float32)

        beam_scores = tf.reshape(beam_scores, (batch_size * num_beams,))

        # cache compute states
        kwargs["encoder_outputs"] = encoder_outputs
        past = kwargs["encoder_outputs"]
        # to stay similar to torch : past = (encoder_outputs, None) if encoder_outputs is not None else None

        # done sentences
        done = [False for _ in range(batch_size)]

        while cur_len < max_length:
            model_inputs = self.prepare_inputs_for_generation(
                input_ids, past=past, attention_mask=attention_mask, use_cache=use_cache, **kwargs
            )
            outputs = self(**model_inputs)  # (batch_size * num_beams, cur_len, vocab_size)
            next_token_logits = outputs.logits[:, -1, :]  # (batch_size * num_beams, vocab_size)

            # if model has past, then set the past variable to speed up decoding
            if self._use_cache(outputs, use_cache):
                past = outputs.past_key_values

            # repetition penalty (from CTRL paper https://arxiv.org/abs/1909.05858)
            if repetition_penalty != 1.0:
                next_token_logits_penalties = _create_next_token_logits_penalties(
                    input_ids, next_token_logits, repetition_penalty
                )
                next_token_logits = tf.math.multiply(next_token_logits, next_token_logits_penalties)

            # Temperature (higher temperature => more likely to sample low probability tokens)
            if temperature != 1.0:
                next_token_logits = next_token_logits / temperature

            if self.config.is_encoder_decoder and do_sample is False:
                next_token_logits = self.adjust_logits_during_generation(
                    next_token_logits, cur_len=cur_len, max_length=max_length
                )
            #             calculate log softmax score
            scores = tf.nn.log_softmax(next_token_logits, axis=-1)  # (batch_size * num_beams, vocab_size)

            # set eos token prob to zero if min_length is not reached
            if eos_token_id is not None and cur_len < min_length:
                # create eos_token_id boolean mask
                num_batch_hypotheses = batch_size * num_beams

                is_token_logit_eos_token = tf.convert_to_tensor(
                    [True if token is eos_token_id else False for token in range(vocab_size)], dtype=tf.bool
                )
                eos_token_indices_mask = tf.broadcast_to(is_token_logit_eos_token, [num_batch_hypotheses, vocab_size])

                scores = set_tensor_by_indices_to_value(scores, eos_token_indices_mask, -float("inf"))

            if no_repeat_ngram_size > 0:
                # calculate a list of banned tokens to prevent repetitively generating the same ngrams
                # from fairseq: https://github.com/pytorch/fairseq/blob/a07cb6f40480928c9e0548b737aadd36ee66ac76/fairseq/sequence_generator.py#L345
                num_batch_hypotheses = batch_size * num_beams
                banned_tokens = calc_banned_ngram_tokens(
                    input_ids, num_batch_hypotheses, no_repeat_ngram_size, cur_len
                )
                # create banned_tokens boolean mask
                banned_tokens_indices_mask = []
                for banned_tokens_slice in banned_tokens:
                    banned_tokens_indices_mask.append(
                        [True if token in banned_tokens_slice else False for token in range(vocab_size)]
                    )

                scores = set_tensor_by_indices_to_value(
                    scores, tf.convert_to_tensor(banned_tokens_indices_mask, dtype=tf.bool), -float("inf")
                )

            if bad_words_ids is not None:
                # calculate a list of banned tokens according to bad words
                banned_tokens = calc_banned_bad_words_ids(input_ids, bad_words_ids)

                banned_tokens_indices_mask = []
                for banned_tokens_slice in banned_tokens:
                    banned_tokens_indices_mask.append(
                        [True if token in banned_tokens_slice else False for token in range(vocab_size)]
                    )

                scores = set_tensor_by_indices_to_value(
                    scores, tf.convert_to_tensor(banned_tokens_indices_mask, dtype=tf.bool), -float("inf")
                )

            assert shape_list(scores) == [batch_size * num_beams, vocab_size]

            if do_sample:
                _scores = scores + tf.broadcast_to(
                    beam_scores[:, None], (batch_size * num_beams, vocab_size)
                )  # (batch_size * num_beams, vocab_size)

                # Top-p/top-k filtering
                _scores = tf_top_k_top_p_filtering(
                    _scores, top_k=top_k, top_p=top_p, min_tokens_to_keep=2
                )  # (batch_size * num_beams, vocab_size)
                # Sample 2 next tokens for each beam (so we have some spare tokens and match output of greedy beam search)
                _scores = tf.reshape(_scores, (batch_size, num_beams * vocab_size))

                next_tokens = sample_without_replacement(
                    _scores, num_samples=2 * num_beams
                )  # (batch_size, 2 * num_beams)
                # Compute next scores
                next_scores = tf.gather(_scores, next_tokens, batch_dims=1)  # (batch_size, 2 * num_beams)

                # sort the sampled vector to make sure that the first num_beams samples are the best
                next_scores_indices = tf.argsort(next_scores, direction="DESCENDING", axis=1)
                next_scores = tf.gather(next_scores, next_scores_indices, batch_dims=1)  # (batch_size, num_beams * 2)
                next_tokens = tf.gather(next_tokens, next_scores_indices, batch_dims=1)  # (batch_size, num_beams * 2)
            else:
                # Add the log prob of the new beams to the log prob of the beginning of the sequence (sum of logs == log of the product)
                next_scores = scores + tf.broadcast_to(
                    beam_scores[:, None], (batch_size * num_beams, vocab_size)
                )  # (batch_size * num_beams, vocab_size)

                # re-organize to group the beam together (we are keeping top hypothesis across beams)
                next_scores = tf.reshape(
                    next_scores, (batch_size, num_beams * vocab_size)
                )  # (batch_size, num_beams * vocab_size)

                next_scores, next_tokens = tf.math.top_k(next_scores, k=2 * num_beams, sorted=True)

            assert shape_list(next_scores) == shape_list(next_tokens) == [batch_size, 2 * num_beams]

            # next batch beam content
            next_batch_beam = []

            # for each sentence
            for batch_idx in range(batch_size):

                # if we are done with this sentence
                if done[batch_idx]:
                    assert (
                        len(generated_hyps[batch_idx]) >= num_beams
                    ), "Batch can only be done if at least {} beams have been generated".format(num_beams)
                    assert (
                        eos_token_id is not None and pad_token_id is not None
                    ), "generated beams >= num_beams -> eos_token_id and pad_token have to be defined"
                    next_batch_beam.extend([(0, pad_token_id, 0)] * num_beams)  # pad the batch
                    continue

                # next sentence beam content
                next_sent_beam = []

                # next tokens for this sentence
                for beam_token_rank, (beam_token_id, beam_token_score) in enumerate(
                    zip(next_tokens[batch_idx], next_scores[batch_idx])
                ):
                    # get beam and token IDs
                    beam_id = beam_token_id // vocab_size
                    token_id = beam_token_id % vocab_size

                    effective_beam_id = batch_idx * num_beams + beam_id
                    # add to generated hypotheses if end of sentence or last iteration
                    if (eos_token_id is not None) and (token_id.numpy() == eos_token_id):
                        # if beam_token does not belong to top num_beams tokens, it should not be added
                        is_beam_token_worse_than_top_num_beams = beam_token_rank >= num_beams
                        if is_beam_token_worse_than_top_num_beams:
                            continue
                        generated_hyps[batch_idx].add(
                            tf.identity(input_ids[effective_beam_id]), beam_token_score.numpy()
                        )
                    else:
                        # add next predicted token if it is not eos_token
                        next_sent_beam.append((beam_token_score, token_id, effective_beam_id))

                    # the beam for next step is full
                    if len(next_sent_beam) == num_beams:
                        break

                # Check if we are done so that we can save a pad step if all(done)
                done[batch_idx] = done[batch_idx] or generated_hyps[batch_idx].is_done(
                    tf.reduce_max(next_scores[batch_idx]).numpy(), cur_len
                )

                # update next beam content
                assert len(next_sent_beam) == num_beams, "Beam should always be full"
                next_batch_beam.extend(next_sent_beam)
                assert len(next_batch_beam) == num_beams * (batch_idx + 1)

            # stop when we are done with each sentence
            if all(done):
                break

            # sanity check / prepare next batch
            assert len(next_batch_beam) == batch_size * num_beams
            beam_scores = tf.convert_to_tensor([x[0] for x in next_batch_beam], dtype=tf.float32)
            beam_tokens = tf.convert_to_tensor([x[1] for x in next_batch_beam], dtype=tf.int32)
            beam_idx = tf.convert_to_tensor([x[2] for x in next_batch_beam], dtype=tf.int32)

            # re-order batch and update current length
            input_ids = tf.stack([tf.identity(input_ids[x, :]) for x in beam_idx])
            input_ids = tf.concat([input_ids, tf.expand_dims(beam_tokens, 1)], axis=-1)
            cur_len = cur_len + 1

            # re-order internal states
            if past is not None:
                past = self._reorder_cache(past, beam_idx)

            # extend attention_mask for new generated input if only decoder
            if self.config.is_encoder_decoder is False:
                attention_mask = tf.concat(
                    [attention_mask, tf.ones((shape_list(attention_mask)[0], 1), dtype=tf.int32)], axis=-1
                )

        # finalize all open beam hypotheses and end to generated hypotheses
        for batch_idx in range(batch_size):
            # Add all open beam hypothesis to generated_hyps
            if done[batch_idx]:
                continue
            # test that beam scores match previously calculated scores if not eos and batch_idx not done
            if eos_token_id is not None and all(
                (token_id % vocab_size).numpy().item() != eos_token_id for token_id in next_tokens[batch_idx]
            ):
                assert tf.reduce_all(
                    next_scores[batch_idx, :num_beams] == tf.reshape(beam_scores, (batch_size, num_beams))[batch_idx]
                ), "If batch_idx is not done, final next scores: {} have to equal to accumulated beam_scores: {}".format(
                    next_scores[:, :num_beams][batch_idx], tf.reshape(beam_scores, (batch_size, num_beams))[batch_idx]
                )

            # need to add best num_beams hypotheses to generated hyps
            for beam_id in range(num_beams):
                effective_beam_id = batch_idx * num_beams + beam_id
                final_score = beam_scores[effective_beam_id].numpy().item()
                final_tokens = input_ids[effective_beam_id]
                generated_hyps[batch_idx].add(final_tokens, final_score)

        # depending on whether greedy generation is wanted or not define different output_batch_size and output_num_return_sequences_per_batch
        output_batch_size = batch_size if do_sample else batch_size * num_return_sequences
        output_num_return_sequences_per_batch = 1 if do_sample else num_return_sequences

        # select the best hypotheses
        sent_lengths_list = []
        best = []

        # retrieve best hypotheses
        for i, hypotheses in enumerate(generated_hyps):
            sorted_hyps = sorted(hypotheses.beams, key=lambda x: x[0])
            for j in range(output_num_return_sequences_per_batch):
                best_hyp = sorted_hyps.pop()[1]
                sent_lengths_list.append(len(best_hyp))
                best.append(best_hyp)
        assert output_batch_size == len(best), "Output batch size {} must match output beam hypotheses {}".format(
            output_batch_size, len(best)
        )

        sent_lengths = tf.convert_to_tensor(sent_lengths_list, dtype=tf.int32)

        # shorter batches are filled with pad_token
        if tf.reduce_min(sent_lengths).numpy() != tf.reduce_max(sent_lengths).numpy():
            assert pad_token_id is not None, "`Pad_token_id` has to be defined"
            sent_max_len = min(tf.reduce_max(sent_lengths).numpy() + 1, max_length)
            decoded_list = []

            # fill with hypothesis and eos_token_id if necessary
            for i, hypo in enumerate(best):
                assert sent_lengths[i] == shape_list(hypo)[0]
                # if sent_length is max_len do not pad
                if sent_lengths[i] == sent_max_len:
                    decoded_slice = hypo
                else:
                    # else pad to sent_max_len
                    num_pad_tokens = sent_max_len - sent_lengths[i]
                    padding = pad_token_id * tf.ones((num_pad_tokens,), dtype=tf.int32)
                    decoded_slice = tf.concat([hypo, padding], axis=-1)

                    # finish sentence with EOS token
                    if sent_lengths[i] < max_length:
                        decoded_slice = tf.where(
                            tf.range(sent_max_len, dtype=tf.int32) == sent_lengths[i],
                            eos_token_id * tf.ones((sent_max_len,), dtype=tf.int32),
                            decoded_slice,
                        )
                # add to list
                decoded_list.append(decoded_slice)

            decoded = tf.stack(decoded_list)
        else:
            # none of the hypotheses have an eos_token
            assert (len(hypo) == max_length for hypo in best)
            decoded = tf.stack(best)

        return decoded

    def _generate_no_beam_search(
        self,
        input_ids,
        cur_len,
        max_length,
        min_length,
        do_sample,
        temperature,
        top_k,
        top_p,
        repetition_penalty,
        no_repeat_ngram_size,
        bad_words_ids,
        pad_token_id,
        eos_token_id,
        batch_size,
        vocab_size,
        encoder_outputs,
        attention_mask,
        use_cache,
        **kwargs
    ):
        """
        Generate sequences for each example without beam search (num_beams == 1). All returned sequence are generated
        independantly.
        """

        # length of generated sentences / unfinished sentences
        unfinished_sents = tf.ones_like(input_ids[:, 0])
        sent_lengths = tf.ones_like(input_ids[:, 0]) * max_length

        past = encoder_outputs  # defined for encoder-decoder models, None for decoder-only models
        kwargs["encoder_outputs"] = encoder_outputs

        while cur_len < max_length:
            model_inputs = self.prepare_inputs_for_generation(
                input_ids, past=past, attention_mask=attention_mask, use_cache=use_cache, **kwargs
            )
            outputs = self(**model_inputs)
            next_token_logits = outputs.logits[:, -1, :]

            # if model has past, then set the past variable to speed up decoding
            if self._use_cache(outputs, use_cache):
                past = outputs.past_key_values

            # repetition penalty from CTRL paper (https://arxiv.org/abs/1909.05858)
            if repetition_penalty != 1.0:
                next_token_logits_penalties = _create_next_token_logits_penalties(
                    input_ids, next_token_logits, repetition_penalty
                )
                next_token_logits = tf.math.multiply(next_token_logits, next_token_logits_penalties)

            if no_repeat_ngram_size > 0:
                # calculate a list of banned tokens to prevent repetitively generating the same ngrams
                # from fairseq: https://github.com/pytorch/fairseq/blob/a07cb6f40480928c9e0548b737aadd36ee66ac76/fairseq/sequence_generator.py#L345
                banned_tokens = calc_banned_ngram_tokens(input_ids, batch_size, no_repeat_ngram_size, cur_len)
                # create banned_tokens boolean mask
                banned_tokens_indices_mask = []
                for banned_tokens_slice in banned_tokens:
                    banned_tokens_indices_mask.append(
                        [True if token in banned_tokens_slice else False for token in range(vocab_size)]
                    )

                next_token_logits = set_tensor_by_indices_to_value(
                    next_token_logits, tf.convert_to_tensor(banned_tokens_indices_mask, dtype=tf.bool), -float("inf")
                )

            if bad_words_ids is not None:
                # calculate a list of banned tokens according to bad words
                banned_tokens = calc_banned_bad_words_ids(input_ids, bad_words_ids)

                banned_tokens_indices_mask = []
                for banned_tokens_slice in banned_tokens:
                    banned_tokens_indices_mask.append(
                        [True if token in banned_tokens_slice else False for token in range(vocab_size)]
                    )

                next_token_logits = set_tensor_by_indices_to_value(
                    next_token_logits, tf.convert_to_tensor(banned_tokens_indices_mask, dtype=tf.bool), -float("inf")
                )

            # set eos token prob to zero if min_length is not reached
            if eos_token_id is not None and cur_len < min_length:
                # create eos_token_id boolean mask
                is_token_logit_eos_token = tf.convert_to_tensor(
                    [True if token is eos_token_id else False for token in range(vocab_size)], dtype=tf.bool
                )
                eos_token_indices_mask = tf.broadcast_to(is_token_logit_eos_token, [batch_size, vocab_size])

                next_token_logits = set_tensor_by_indices_to_value(
                    next_token_logits, eos_token_indices_mask, -float("inf")
                )

            if do_sample:
                # Temperature (higher temperature => more likely to sample low probability tokens)
                if temperature != 1.0:
                    next_token_logits = next_token_logits / temperature
                # Top-p/top-k filtering
                next_token_logits = tf_top_k_top_p_filtering(next_token_logits, top_k=top_k, top_p=top_p)
                # Sample
                next_token = tf.squeeze(
                    tf.random.categorical(next_token_logits, dtype=tf.int32, num_samples=1), axis=1
                )
            else:
                # Greedy decoding
                next_token = tf.math.argmax(next_token_logits, axis=-1, output_type=tf.int32)

            # update generations and finished sentences
            if eos_token_id is not None:
                # pad finished sentences if eos_token_id exist
                tokens_to_add = next_token * unfinished_sents + (pad_token_id) * (1 - unfinished_sents)
            else:
                tokens_to_add = next_token

            # add token and increase length by one
            input_ids = tf.concat([input_ids, tf.expand_dims(tokens_to_add, -1)], 1)
            cur_len = cur_len + 1

            if eos_token_id is not None:
                eos_in_sents = tokens_to_add == eos_token_id
                # if sentence is unfinished and the token to add is eos, sent_lengths is filled with current length
                is_sents_unfinished_and_token_to_add_is_eos = tf.math.multiply(
                    unfinished_sents, tf.cast(eos_in_sents, tf.int32)
                )
                sent_lengths = (
                    sent_lengths * (1 - is_sents_unfinished_and_token_to_add_is_eos)
                    + cur_len * is_sents_unfinished_and_token_to_add_is_eos
                )

                # unfinished_sents is set to zero if eos in sentence
                unfinished_sents -= is_sents_unfinished_and_token_to_add_is_eos

            # stop when there is a </s> in each sentence, or if we exceed the maximul length
            if tf.math.reduce_max(unfinished_sents) == 0:
                break

            # extend attention_mask for new generated input if only decoder
            if self.config.is_encoder_decoder is False:
                attention_mask = tf.concat(
                    [attention_mask, tf.ones((shape_list(attention_mask)[0], 1), dtype=tf.int32)], axis=-1
                )

        # if there are different sentences lengths in the batch, some batches have to be padded
        min_sent_length = tf.math.reduce_min(sent_lengths)
        max_sent_length = tf.math.reduce_max(sent_lengths)

        if min_sent_length != max_sent_length:
            assert pad_token_id is not None, "`Pad_token_id` has to be defined if batches have different lengths"
            # finished sents are filled with pad_token
            padding = tf.ones([batch_size, max_sent_length.numpy()], dtype=tf.int32) * pad_token_id

            # create length masks for tf.where operation
            broad_casted_sent_lengths = tf.broadcast_to(
                tf.expand_dims(sent_lengths, -1), [batch_size, max_sent_length]
            )
            broad_casted_range = tf.transpose(
                tf.broadcast_to(tf.expand_dims(tf.range(max_sent_length), -1), [max_sent_length, batch_size])
            )

            decoded = tf.where(broad_casted_range < broad_casted_sent_lengths, input_ids, padding)
        else:
            decoded = input_ids

        return decoded

    def get_input_embeddings(self):
        return self.rag.generator.get_input_embeddings()

    def get_output_embeddings(self):
        return self.rag.generator.get_output_embeddings()

    # Adapted from tf_t5's & tf_bart's _shift_right
    def shift_tokens_right(self, input_ids, start_token_id=None):
        """Shift input ids one token to the right, and pad with start_token_id"""

        if start_token_id is None:
            start_token_id = self.generator.config.decoder_start_token_id
            assert (
                start_token_id is not None
            ), "self.generator.config.decoder_start_token_id has to be defined. In Rag we commonly use Bart as generator, see Bart docs for more information"

        pad_token_id = self.generator.config.pad_token_id
        assert pad_token_id is not None, "self.model.config.pad_token_id has to be defined."

        shifted_input_ids = tf.cast(input_ids, tf.int32)
        shifted_input_ids = tf.roll(shifted_input_ids, 1, axis=-1)
        start_tokens = tf.fill((shape_list(shifted_input_ids)[0], 1), start_token_id)
        shifted_input_ids = tf.concat([start_tokens, shifted_input_ids[:, 1:]], -1)

        # replace possible -100 values in labels by `pad_token_id`
        shifted_input_ids = tf.where(
            shifted_input_ids == -100, tf.fill(shape_list(shifted_input_ids), pad_token_id), shifted_input_ids
        )

        # "Verify that `labels` has only positive values and -100"
        assert_gte0 = tf.debugging.assert_greater_equal(shifted_input_ids, tf.cast(0, tf.int32))

        # Make sure the assertion op is called by wrapping the result in an identity no-op
        with tf.control_dependencies([assert_gte0]):
            shifted_input_ids = tf.identity(shifted_input_ids)

        return shifted_input_ids

    # nll stands for 'negative log likelihood'
    def get_nll(self, seq_logits, doc_scores, target, reduce_loss=False, epsilon=0.0, n_docs=None):
        n_docs = n_docs if n_docs is not None else self.config.n_docs
        # shift tokens left (from original Pytorch's version)
        # CONCERNS : T5 shift-right, RAG shift-left -> inconsistent label format ?
        target = tf.concat([target[:, 1:], tf.fill([target.shape[0], 1], self.config.generator.pad_token_id)], axis=1)
        rag_logprobs = self.marginalize(seq_logits, doc_scores, n_docs)
        loss = self.compute_loss(target, rag_logprobs, from_logits=True, reduce_loss=reduce_loss)

        return loss

    # Adopted modeling_tf_bart + add smooth_loss to match with pytorch version
    def compute_loss(self, labels, y_pred, smooth_epsilon=0.0, from_logits=True, reduce_loss=False):
        """CrossEntropyLoss that ignores pad tokens"""
        loss_fn = tf.keras.losses.SparseCategoricalCrossentropy(
            from_logits=True,
            reduction=tf.keras.losses.Reduction.SUM,
        )

        if from_logits == False:  # convert to logits
            eps = 1e-9
            y_pred = tf.clip_by_value(y_pred, clip_value_min=eps, clip_value_max=1 - eps)
            y_pred = tf.math.log(y_pred)

        logits = y_pred
        melted_labels = tf.reshape(labels, (-1,))
        active_loss = tf.not_equal(melted_labels, self.config.generator.pad_token_id)

        reduced_logits = tf.boolean_mask(tf.reshape(logits, (-1, logits.shape[2])), active_loss)
        labels = tf.boolean_mask(melted_labels, active_loss)
        nll_loss = loss_fn(labels, reduced_logits)

        smooth_loss = -tf.reduce_sum(reduced_logits, axis=-1)
        smooth_loss = tf.reduce_sum(smooth_loss)  # sum and squeeze like torch
        eps_i = smooth_epsilon / reduced_logits.shape[-1]

        loss = (1.0 - smooth_epsilon) * nll_loss + eps_i * smooth_loss

        return loss


@add_start_docstrings_to_model_forward(
    """
    A TF RAG-sequence model implementation. It performs RAG-sequence specific marginalization in the forward pass.
    """,
    RAG_START_DOCSTRING,
)
class TFRagSequenceForGeneration(TFRagPreTrainedModel, TFCausalLanguageModelingLoss):

    load_weight_prefix = "tf_rag_sequence_for_generation_1/rag"

    def __init__(
        self,
        config: Optional[PretrainedConfig] = None,
        question_encoder: Optional[TFPreTrainedModel] = None,
        generator: Optional[TFPreTrainedModel] = None,
        retriever: Optional = None,
        **kwargs,
    ):
        assert config is not None or (
            question_encoder is not None and generator is not None
        ), "Either a configuration or an encoder and a generator has to be provided."

        if config is None:
            config = RagConfig.from_question_encoder_generator_configs(
                question_encoder.config, generator.config, **kwargs
            )

        super().__init__(config)

        # instantiate model
        self.rag = TFRagModel(
            config=config,
            question_encoder=question_encoder,
            generator=generator,
            retriever=retriever,
            load_weight_prefix=self.load_weight_prefix,
            name="rag",
        )

    def set_retriever(self, retriever: RagRetriever):
        self.rag.retriever = retriever

    @property
    def retriever(self):
        return self.rag.retriever

    @property
    def generator(self):
        return self.rag.generator

    @property
    def question_encoder(self):
        return self.rag.question_encoder

    @add_start_docstrings_to_model_forward(RAG_FORWARD_INPUTS_DOCSTRING)
    @replace_return_docstrings(output_type=TFRetrievAugLMMarginOutput, config_class=_CONFIG_FOR_DOC)
    def call(
        self,
        input_ids=None,
        attention_mask=None,
        decoder_input_ids=None,
        decoder_attention_mask=None,
        encoder_outputs=None,
        past_key_values=None,
        doc_scores=None,
        context_input_ids=None,
        context_attention_mask=None,
        use_cache=None,
        output_attentions=None,
        output_hidden_states=None,
        output_retrieved=None,
        n_docs=None,
        exclude_bos_score=None,
        labels=None,
        reduce_loss=None,
        training=False,
        **kwargs  # needs kwargs for generation
    ):
        r"""
        exclude_bos_score (:obj:`bool`, `optional`):
            Only relevant if ``labels`` is passed. If :obj:`True`, the score of the BOS token is disregarded when
            computing the loss.
        reduce_loss (:obj:`bool`, `optional`):
            Only relevant if ``labels`` is passed. If :obj:`True`, the NLL loss is reduced using the ``tf.Tensor.sum``
            operation.
        kwargs (:obj:`Dict[str, any]`, optional, defaults to `{}`):
            Legacy dictionary, which is required so that model can use `generate()` function.

        Returns:

        Example::

            >>> from transformers import RagTokenizer, RagRetriever, TFRagSequenceForGeneration

            >>> tokenizer = RagTokenizer.from_pretrained("facebook/rag-sequence-nq")
            >>> retriever = RagRetriever.from_pretrained("facebook/rag-sequence-nq", index_name="exact", use_dummy_dataset=True)
            >>> # initialize with RagRetriever to do everything in one forward call
            >>> model = TFRagRagSequenceForGeneration.from_pretrained("facebook/rag-sequence-nq", retriever=retriever, from_pt=True)

            >>> input_dict = tokenizer.prepare_seq2seq_batch("How many people live in Paris?", "In Paris, there are 10 million people.", return_tensors="tf")
            >>> outputs = model(input_dict, output_retrieved=True)

            >>> # or use retriever separately
            >>> # 1. Encode
            >>> input_ids = input_dict["input_ids"]
            >>> question_hidden_states = model.question_encoder(input_ids)[0]
            >>> # 2. Retrieve
            >>> docs_dict = retriever(input_ids.numpy(), question_hidden_states.numpy(), return_tensors="tf")
            >>> doc_scores = tf.squeeze(tf.matmul(tf.expand_dims(question_hidden_states, axis=[1]), docs_dict["retrieved_doc_embeds"], transpose_b=True), axis=[1])
            >>> # 3. Forward to generator
            >>> outputs = model(inputs=None, context_input_ids=docs_dict["context_input_ids"], context_attention_mask=docs_dict["context_attention_mask"], doc_scores=doc_scores, decoder_input_ids=input_dict["labels"])

            >>> # or directly generate
            >>> generated = model.generate(context_input_ids=docs_dict["context_input_ids"], context_attention_mask=docs_dict["context_attention_mask"], doc_scores=doc_scores)
            >>> generated_string = tokenizer.batch_decode(generated, skip_special_tokens=True)
        """

        assert (
            "decoder_cached_states" not in kwargs
        ), "Please use past_key_values to cache intermediate outputs"  # from modeling_tf_bart.py

        inputs = input_processing(
            func=self.call,
            config=self.config,
            input_ids=input_ids,
            attention_mask=attention_mask,
            decoder_input_ids=decoder_input_ids,
            decoder_attention_mask=decoder_attention_mask,
            encoder_outputs=encoder_outputs,
            past_key_values=past_key_values,
            doc_scores=doc_scores,
            context_input_ids=context_input_ids,
            context_attention_mask=context_attention_mask,
            use_cache=use_cache,
            output_attentions=output_attentions,
            output_hidden_states=output_hidden_states,
            output_retrieved=output_retrieved,
            n_docs=n_docs,
            exclude_bos_score=exclude_bos_score,
            labels=labels,
            reduce_loss=reduce_loss,
            training=training,
            kwargs_call=kwargs,
        )

        inputs["exclude_bos_score"] = (
            inputs["exclude_bos_score"] if inputs["exclude_bos_score"] else self.config.exclude_bos_score
        )
        inputs["reduce_loss"] = inputs["reduce_loss"] if inputs["reduce_loss"] else self.config.reduce_loss

        if inputs["labels"] is not None:
            if inputs["decoder_input_ids"] is None:
                inputs["decoder_input_ids"] = inputs["labels"]
            inputs["use_cache"] = False

        outputs = self.rag(
            inputs["input_ids"],
            attention_mask=inputs["attention_mask"],
            encoder_outputs=inputs["encoder_outputs"],
            decoder_input_ids=inputs["decoder_input_ids"],
            decoder_attention_mask=inputs["decoder_attention_mask"],
            context_input_ids=inputs["context_input_ids"],
            context_attention_mask=inputs["context_attention_mask"],
            doc_scores=inputs["doc_scores"],
            past_key_values=inputs["past_key_values"],
            use_cache=inputs["use_cache"],
            output_attentions=inputs["output_attentions"],
            output_hidden_states=inputs["output_hidden_states"],
            output_retrieved=inputs["output_retrieved"],
            n_docs=inputs["n_docs"],
            training=inputs["training"],
        )

        loss = None
        if inputs["labels"] is not None:
            loss = self.get_nll(
                outputs.logits,
                outputs.doc_scores,
                inputs["labels"],
                reduce_loss=inputs["reduce_loss"],
                epsilon=self.config.label_smoothing,
                n_docs=inputs["n_docs"],
            )

        return TFRetrievAugLMMarginOutput(
            loss=loss,
            logits=outputs.logits,
            doc_scores=outputs.doc_scores,
            past_key_values=outputs.past_key_values,
            context_input_ids=outputs.context_input_ids,
            context_attention_mask=outputs.context_attention_mask,
            retrieved_doc_embeds=outputs.retrieved_doc_embeds,
            retrieved_doc_ids=outputs.retrieved_doc_ids,
            question_encoder_last_hidden_state=outputs.question_encoder_last_hidden_state,
            question_enc_hidden_states=outputs.question_enc_hidden_states,
            question_enc_attentions=outputs.question_enc_attentions,
            generator_enc_last_hidden_state=outputs.generator_enc_last_hidden_state,
            generator_enc_hidden_states=outputs.generator_enc_hidden_states,
            generator_enc_attentions=outputs.generator_enc_attentions,
            generator_dec_hidden_states=outputs.generator_dec_hidden_states,
            generator_dec_attentions=outputs.generator_dec_attentions,
        )

    def get_nll(
        self, seq_logits, doc_scores, target, reduce_loss=False, epsilon=0.0, exclude_bos_score=False, n_docs=None
    ):
        # shift tokens left
        target = tf.concat([target[:, 1:], tf.fill([target.shape[0], 1], self.config.generator.pad_token_id)], axis=1)

        # bos_token_id is None for T5
        bos_token_id = self.config.bos_token_id or self.config.generator.bos_token_id
        n_docs = n_docs if n_docs is not None else self.config.n_docs
        equal_bos_token_id_all = tf.reduce_all(tf.equal(target[:, 0], bos_token_id))
        use_bos = bos_token_id is not None and equal_bos_token_id_all

        def _mask_pads(ll, smooth_obj):
            pad_mask = tf.equal(target, self.config.generator.pad_token_id)
            if tf.reduce_any(pad_mask):
                ll = tf.where(pad_mask, 0.0, ll)
                smooth_obj = tf.where(pad_mask, 0.0, smooth_obj)
            return tf.squeeze(ll, axis=-1), tf.squeeze(smooth_obj, axis=-1)

        # seq_logits.shape = (batch*n_docs, tgt_len , vocabs)
        seq_logprobs = tf.nn.log_softmax(seq_logits, axis=-1)
        seq_logprobs = tf.reshape(
            seq_logprobs, (seq_logits.shape[0] // n_docs, n_docs, -1, seq_logits.shape[-1])
        )  # (batch_size, n_docs, tgt_len, vocabs)
        doc_logprobs = tf.nn.log_softmax(doc_scores, axis=1)
        doc_logprobs = tf.expand_dims(doc_logprobs, axis=-1)
        doc_logprobs = tf.expand_dims(doc_logprobs, axis=-1)  # done twice to get 4-D

        # RAG-sequence marginalization
        first_token_scores = seq_logprobs[:, :, :1, :]
        second_token_scores = seq_logprobs[:, :, 1:2, :]
        remainder = seq_logprobs[:, :, 2:, :]
        rag_logprobs = tf.concat([first_token_scores, second_token_scores + doc_logprobs, remainder], axis=2)

        # calculate loss
        target = tf.expand_dims(target, axis=1)  # n_docs dimension
        target = tf.expand_dims(target, axis=-1)  # logits dimension
        target = tf.repeat(target, n_docs, axis=1)
        assert len(target.shape) == len(rag_logprobs.shape)

        # last-axis gathering only - use 2D-reshape-trick for Torch's style nD gathering
        def torch_gather(param, id_tensor):
            # 2d-gather torch equivalent: https://stackoverflow.com/questions/52129909/tensorflow-equivalent-of-torch-gather
            def gather2d(target, id_tensor):
                idx = tf.stack([tf.range(tf.shape(id_tensor)[0]), id_tensor[:, 0]], axis=-1)
                result = tf.gather_nd(target, idx)
                return tf.expand_dims(result, axis=-1)

            target = tf.reshape(param, (-1, param.shape[-1]))  # reshape 2D
            target_shape = id_tensor.shape

            id_tensor = tf.reshape(id_tensor, (-1, 1))  # also 2D-index
            result = gather2d(target, id_tensor)
            return tf.reshape(result, target_shape)

        ll = torch_gather(rag_logprobs, id_tensor=target)
        smooth_obj = tf.reduce_sum(rag_logprobs, axis=-1, keepdims=True)  # total sum of all (normalised) logits

        ll, smooth_obj = _mask_pads(ll, smooth_obj)

        # sum over tokens, exclude bos while scoring
        if exclude_bos_score and use_bos:
            ll = tf.reduce_sum(ll[:, :, 1:], axis=2)
        else:
            ll = tf.reduce_sum(ll, axis=2)

        smooth_obj = tf.reduce_sum(smooth_obj, axis=2)
        ll = tf.math.reduce_logsumexp(ll, axis=1)  # logsumexp over docs
        smooth_obj = tf.math.reduce_logsumexp(smooth_obj, axis=1)

        nll_loss = -ll
        smooth_loss = -smooth_obj

        if reduce_loss:
            nll_loss = tf.reduce_sum(nll_loss)
            smooth_loss = tf.reduce_sum(smooth_loss)

        eps_i = epsilon / rag_logprobs.shape[-1]
        loss = (1.0 - epsilon) * nll_loss + eps_i * smooth_loss
        return loss

    def generate(
        self,
        input_ids: Optional[tf.Tensor] = None,
        attention_mask: Optional[tf.Tensor] = None,
        context_input_ids=None,
        context_attention_mask=None,
        doc_scores=None,
        do_deduplication=None,  # defaults to True
        num_return_sequences=None,  # defaults to 1
        num_beams=None,  # defaults to 1
        n_docs=None,
        **model_kwargs
    ):
        """
        Implements RAG sequence "thorough" decoding. Read the :meth:`~transformers.PreTrainedModel.generate``
        documentation for more information on how to set other generate input parameters

        Args:
            input_ids (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
                The sequence used as a prompt for the generation. If :obj:`input_ids` is not passed, then
                :obj:`context_input_ids` has to be provided.
            attention_mask (:obj:`tf.Tensor` of shape :obj:`(batch_size, sequence_length)`, `optional`):
                Mask to avoid performing attention on padding token indices. Mask values selected in ``[0, 1]``: - 1
                for tokens that are **not masked**, - 0 for tokens that are **masked**. `What are attention masks?
                <../glossary.html#attention-mask>`__
            context_input_ids (:obj:`tf.Tensor` of shape :obj:`(batch_size * config.n_docs, config.max_combined_length)`, `optional`, returned when `output_retrieved=True`):
                Input IDs post-processed from the retrieved documents and the question encoder input_ids by the
                retriever.
            context_attention_mask (:obj:`tf.Tensor` of shape :obj:`(batch_size * config.n_docs, config.max_combined_length)`, `optional`, returned when `output_retrieved=True`):
                Attention mask post-processed from the retrieved documents and the question encoder :obj:`input_ids` by
                the retriever. If the model has is not initialized with a ``retriever`` or ``input_ids`` is not given,
                :obj:`context_input_ids` and :obj:`context_attention_mask` have to be provided to the forward pass.
                They are returned by :meth:`~transformers.RagRetriever.__call__`.
            doc_scores (:obj:`tf.Tensor` of shape :obj:`(batch_size, config.n_docs)`):
                Score between each retrieved document embeddings (see :obj:`retrieved_doc_embeds`) and
                :obj:`question_encoder_last_hidden_state`. If the model has is not initialized with a ``retriever`` or
                ``input_ids`` is not given, :obj:`doc_scores` has to be provided to the forward pass. :obj:`doc_scores`
                are returned by :meth:`~transformers.RagRetriever.__call__`.
            do_deduplication (:obj:`bool`, `optional`):
                Whether or not to deduplicate the generations from different context documents for a given input. Has
                to be set to :obj:`False` if used while training with distributed backend.
            num_return_sequences(:obj:`int`, `optional`, defaults to 1):
                The number of independently computed returned sequences for each element in the batch. Note that this
                is not the value we pass to the ``generator``'s `:func:`~transformers.PreTrainedModel.generate``
                function, where we set ``num_return_sequences`` to :obj:`num_beams`.
            num_beams (:obj:`int`, `optional`, defaults to 1):
                Number of beams for beam search. 1 means no beam search.
            n_docs (:obj:`int`, `optional`, defaults to :obj:`config.n_docs`)
                Number of documents to retrieve and/or number of documents for which to generate an answer.
            kwargs:
                Additional kwargs will be passed to :meth:`~transformers.PreTrainedModel.generate`

        Return:
            :obj:`tf.Tensor` of shape :obj:`(batch_size * num_return_sequences, sequence_length)`: The generated
            sequences. The second dimension (sequence length) is either equal to :obj:`max_length` or shorter if all
            batches finished early due to the :obj:`eos_token_id`.
        """

        n_docs = n_docs if n_docs is not None else self.config.n_docs
        do_deduplication = do_deduplication if do_deduplication is not None else self.config.do_deduplication
        num_doc_return_sequences = (
            num_return_sequences if num_return_sequences is not None else self.config.num_return_sequences
        )
        num_beams = num_beams if num_beams is not None else self.config.num_beams

        assert (
            input_ids is not None or context_input_ids is not None
        ), " At least one of input_ids or context_input_ids must be given"

        if self.retriever is not None and context_input_ids is None:
            question_hidden_states = self.question_encoder(input_ids, attention_mask=attention_mask)[0]
            context_input_ids = self.retriever(
                input_ids,
                question_hidden_states.numpy(),
                prefix=self.generator.config.prefix,
                n_docs=n_docs,
                return_tensors="tf",
            )["context_input_ids"]

        hypos = []
        model_kwargs["num_beams"] = num_beams
        model_kwargs["num_return_sequences"] = num_beams  # put here so that not confused with num_doc_return_sequences
        model_kwargs["attention_mask"] = None

        batch_size = input_ids.shape[0] if input_ids is not None else context_input_ids.shape[0] // n_docs

        for index in range(batch_size):
            # first, generate beams from documents:
            generator_input_ids = context_input_ids[index * n_docs : (index + 1) * n_docs]  # (n_docs, max_len)

            output_sequences = self.generator.generate(
                generator_input_ids,
                **model_kwargs,
            )  # n_docs * n_beam, tgt_len
            if do_deduplication:
                # do_deduplication -- for TF, work on Eager mode only!
                output_sequences = tf.stack(list({str(k.numpy().tolist()): k for k in output_sequences}.values()))

            num_candidates = output_sequences.shape[
                0
            ]  # after deduplication, this number can be less than n_docs*n_beam

            # then, run model forwards to get nll scores:
            if input_ids is not None:
                new_input_ids = tf.tile(input_ids[index : index + 1], (num_candidates, 1))
                outputs = self(new_input_ids, labels=output_sequences, exclude_bos_score=True)
            else:  # input_ids is None, need context_input_ids/mask and doc_scores
                assert (
                    context_attention_mask is not None
                ), "Make sure that `context_attention_mask` are passed, if no `input_ids` is set. Alternatively, you can set a retriever using the `set_retriever(...)` function."
                assert (
                    doc_scores is not None
                ), "Make sure that `doc_scores` are passed, if no `input_ids` is set. Alternatively, you can set a retriever using the `set_retriever(...)` function."

                individual_input_ids = tf.tile(
                    generator_input_ids, (num_candidates, 1)
                )  # (num_candidates*n_docs, max_len)

                individual_attention_mask = context_attention_mask[index * n_docs : (index + 1) * n_docs]
                individual_attention_mask = tf.tile(individual_attention_mask, (num_candidates, 1))

                individual_doc_scores = doc_scores[index : (index + 1), :]  # doc_scores.shape = [batch, n_docs]
                individual_doc_scores = tf.tile(individual_doc_scores, (num_candidates, 1))  # [num_candidates, n_docs]

                outputs = self(
                    input_ids=None,
                    context_input_ids=individual_input_ids,
                    context_attention_mask=individual_attention_mask,
                    doc_scores=individual_doc_scores,
                    labels=output_sequences,
                    exclude_bos_score=True,
                )

            top_cand_inds = tf.math.top_k((-outputs["loss"]), k=num_doc_return_sequences)[1]

            # add hypothesis
            hypos.append(tf.gather(output_sequences, top_cand_inds))

        return self._cat_and_pad(hypos, pad_token_id=self.config.generator.pad_token_id)

    @staticmethod
    def _cat_and_pad(tensors, pad_token_id):
        # used by generate(): tensors is a (batched) list of (candidates, len); len is varied across batch

        # Initialize padded tensor with shape ( all_candidates , max_candidate_length ),
        # where all_candidates counted from all inputs
        new_shape = sum([t.shape[0] for t in tensors]), max([t.shape[1] for t in tensors])
        output = tf.fill(new_shape, pad_token_id)

        # Normal tensor doesn't support slice assignment, so we need tf.Variable
        output = tf.Variable(output)

        # Assign, and then convert back to tensor
        ind = 0
        for t in tensors:
            output[ind : ind + t.shape[0], : t.shape[1]].assign(t)
            ind += t.shape[0]

        output = tf.convert_to_tensor(output)
        return tf.cast(output, tensors[0][0][0].dtype)
