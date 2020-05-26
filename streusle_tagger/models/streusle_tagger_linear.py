from typing import Any, Dict, List, Optional, Set, Tuple
import json
import logging

from allennlp.common.checks import check_dimensions_match, ConfigurationError
from allennlp.data import Vocabulary
from allennlp.models.model import Model
from allennlp.modules import FeedForward, Seq2SeqEncoder, TimeDistributed, TextFieldEmbedder
from allennlp.nn import InitializerApplicator, RegularizerApplicator
from allennlp.training.metrics import CategoricalAccuracy
import allennlp.nn.util as util
import numpy
from overrides import overrides
import torch
from torch.nn.modules.linear import Linear
import torch.nn.functional as F


ALL_UPOS = {'X', 'INTJ', 'VERB', 'ADV', 'CCONJ', 'PUNCT', 'ADP',
            'NOUN', 'SYM', 'ADJ', 'PROPN', 'DET', 'PART', 'PRON', 'SCONJ', 'NUM', 'AUX'}
ALL_LEXCATS = {'N', 'INTJ', 'INF.P', 'V', 'AUX', 'PP', 'PUNCT',
               'POSS', 'X', 'PRON.POSS', 'SYM', 'PRON', 'SCONJ',
               'NUM', 'DISC', 'ADV', 'CCONJ', 'P', 'ADJ', 'DET', 'INF'}
SPECIAL_LEMMAS = {"to", "be", "versus"}
logger = logging.getLogger(__name__)  # pylint: disable=invalid-name

@Model.register("streusle_tagger_linear")
class StreusleTaggerLinear(Model):
    """
    The ``StreusleTaggerLinear`` embeds a sequence of text before (optionally)
    encoding it with a ``Seq2SeqEncoder`` and passing it through a ``FeedForward``
    before using a linear layer to predict a STREUSLE lextag for
    each token in the sequence.

    Parameters
    ----------
    vocab : ``Vocabulary``, required
        A Vocabulary, required in order to compute sizes for input/output projections.
    text_field_embedder : ``TextFieldEmbedder``, required
        Used to embed the tokens ``TextField`` we get as input to the model.
    encoder : ``Seq2SeqEncoder``, optional (default=``None``)
        The encoder that we will use in between embedding tokens and predicting output tags.
    label_namespace : ``str``, optional (default=``labels``)
        This is needed to constrain the model.
        Unless you did something unusual, the default value should be what you want.
    feedforward : ``FeedForward``, optional, (default = None).
        An optional feedforward layer to apply after the encoder.
    dropout:  ``float``, optional (default=``None``)
    use_upos_constraints : ``bool``, optional (default=``True``)
        Whether to use UPOS constraints. If True, model shoudl recieve UPOS as input.
    use_lemma_constraints : ``bool``, optional (default=``True``)
        Whether to use lemma constraints. If True, model shoudl recieve lemmas as input.
        If this is true, then use_upos_constraints must be true as well.
    train_with_constraints : ``bool``, optional (default=``True``)
        Whether to use the constraints during training, or only during testing.
    initializer : ``InitializerApplicator``, optional (default=``InitializerApplicator()``)
        Used to initialize the model parameters.
    regularizer : ``RegularizerApplicator``, optional (default=``None``)
        If provided, will be used to calculate the regularization penalty during training.

    """

    def __init__(self, vocab: Vocabulary,
                 text_field_embedder: TextFieldEmbedder,
                 encoder: Seq2SeqEncoder = None,
                 label_namespace: str = "labels",
                 feedforward: Optional[FeedForward] = None,
                 dropout: Optional[float] = None,
                 use_upos_constraints: bool = True,
                 use_lemma_constraints: bool = True,
                 train_with_constraints: bool = True,
                 initializer: InitializerApplicator = InitializerApplicator(),
                 regularizer: Optional[RegularizerApplicator] = None) -> None:
        super().__init__(vocab, regularizer)

        self.label_namespace = label_namespace
        self.text_field_embedder = text_field_embedder
        self.num_tags = self.vocab.get_vocab_size(label_namespace)
        self.train_with_constraints = train_with_constraints

        self.encoder = encoder
        if self.encoder is not None:
            encoder_output_dim = self.encoder.get_output_dim()
        else:
            encoder_output_dim = self.text_field_embedder.get_output_dim()
        if dropout:
            self.dropout = torch.nn.Dropout(dropout)
        else:
            self.dropout = None
        self.feedforward = feedforward

        if feedforward is not None:
            output_dim = feedforward.get_output_dim()
        else:
            output_dim = encoder_output_dim
        self.tag_projection_layer = TimeDistributed(Linear(output_dim,
                                                           self.num_tags))
        self._label_namespace = label_namespace
        labels = self.vocab.get_index_to_token_vocabulary(self._label_namespace)
        self.use_upos_constraints = use_upos_constraints
        self.use_lemma_constraints = use_lemma_constraints

        if self.use_lemma_constraints and not self.use_upos_constraints:
            raise ConfigurationError("If lemma constraints are applied, UPOS constraints must be applied as well.")

        if self.use_upos_constraints:
            # Get a dict with a mapping from UPOS to allowed LEXCAT here.
            self._upos_to_allowed_lexcats: Dict[str, Set[str]] = get_upos_allowed_lexcats(
                    stronger_constraints=self.use_lemma_constraints)
            # Dict with a amapping from UPOS to dictionary of [UPOS, list of additionally allowed LEXCATS]
            self._lemma_to_allowed_lexcats: Dict[str, Dict[str, List[str]]] = get_lemma_allowed_lexcats()

            # Use labels and the upos_to_allowed_lexcats to get a dict with
            # a mapping from UPOS to a mask with 1 at allowed label indices and 0 at
            # disallowed label indices.
            self._upos_to_label_mask: Dict[str, torch.Tensor] = {}
            for upos in ALL_UPOS:
                # Shape: (num_labels,)
                upos_label_mask = torch.zeros(len(labels),
                                              device=next(self.tag_projection_layer.parameters()).device)
                # Go through the labels and indices and fill in the values that are allowed.
                for label_index, label in labels.items():
                    if len(label.split("-")) == 1:
                        upos_label_mask[label_index] = 1
                        continue
                    label_lexcat = label.split("-")[1]
                    if not label.startswith("O-") and not label.startswith("o-"):
                        # Label does not start with O-/o-, always allowed.
                        upos_label_mask[label_index] = 1
                    elif label_lexcat in self._upos_to_allowed_lexcats[upos]:
                        # Label starts with O-/o-, but the lexcat is in allowed
                        # lexcats for the current upos.
                        upos_label_mask[label_index] = 1
                self._upos_to_label_mask[upos] = upos_label_mask

            # Use labels and the lemma_to_allowed_lexcats to get a dict with
            # a mapping from lemma to a mask with 1 at an _additionally_ allowed label index
            # and 0 at disallowed label indices. If lemma_to_label_mask has a 0, and upos_to_label_mask
            # has a 0, the lexcat is not allowed for the (upos, lemma). If either lemma_to_label_mask or
            # upos_to_label_mask has a 1, the lexcat is allowed for the (upos, lemma) pair.
            self._lemma_upos_to_label_mask: Dict[Tuple[str, str], torch.Tensor] = {}
            for lemma in SPECIAL_LEMMAS:
                for upos_tag in ALL_UPOS:
                    # No additional constraints, should be all zero
                    if upos_tag not in self._lemma_to_allowed_lexcats[lemma]:
                        continue
                    # Shape: (num_labels,)
                    lemma_upos_label_mask = torch.zeros(len(labels),
                                                        device=next(self.tag_projection_layer.parameters()).device)
                    # Go through the labels and indices and fill in the values that are allowed.
                    for label_index, label in labels.items():
                        # For ~i, etc. tags. We don't deal with them here.
                        if len(label.split("-")) == 1:
                            continue
                        label_lexcat = label.split("-")[1]
                        if not label.startswith("O-") and not label.startswith("o-"):
                            # Label does not start with O-/o-, so we don't deal with it here
                            continue
                        if label_lexcat in self._lemma_to_allowed_lexcats[lemma][upos_tag]:
                            # Label starts with O-/o-, but the lexcat is in allowed
                            # lexcats for the current upos.
                            lemma_upos_label_mask[label_index] = 1
                    self._lemma_upos_to_label_mask[(lemma, upos_tag)] = lemma_upos_label_mask

        self.accuracy_metrics = {
                "accuracy": CategoricalAccuracy(),
                "accuracy3": CategoricalAccuracy(top_k=3)
        }
        if encoder is not None:
            check_dimensions_match(text_field_embedder.get_output_dim(), encoder.get_input_dim(),
                                   "text field embedding dim", "encoder input dim")
        if feedforward is not None:
            check_dimensions_match(encoder.get_output_dim(), feedforward.get_input_dim(),
                                   "encoder output dim", "feedforward input dim")
        initializer(self)

    @overrides
    def forward(self,  # type: ignore
                tokens: Dict[str, torch.LongTensor],
                tags: torch.LongTensor = None,
                metadata: List[Dict[str, Any]] = None) -> Dict[str, torch.Tensor]:
        # pylint: disable=arguments-differ
        """
        Parameters
        ----------
        tokens : ``Dict[str, torch.LongTensor]``, required
            The output of ``TextField.as_array()``, which should typically be passed directly to a
            ``TextFieldEmbedder``. This output is a dictionary mapping keys to ``TokenIndexer``
            tensors.  At its most basic, using a ``SingleIdTokenIndexer`` this is: ``{"tokens":
            Tensor(batch_size, num_tokens)}``. This dictionary will have the same keys as were used
            for the ``TokenIndexers`` when you created the ``TextField`` representing your
            sequence.  The dictionary is designed to be passed directly to a ``TextFieldEmbedder``,
            which knows how to combine different word representations into a single vector per
            token in your input.
        tags : ``torch.LongTensor``, optional (default = ``None``)
            A torch tensor representing the sequence of integer gold lextags of shape
            ``(batch_size, num_tokens)``.
        metadata : ``List[Dict[str, Any]]``, optional, (default = None)
            Additional information about the example.

        Returns
        -------
        An output dictionary consisting of:

        constrained_logits : ``torch.FloatTensor``
            The constrained logits that are the output of the ``tag_projection_layer``
        mask : ``torch.LongTensor``
            The text field mask for the input tokens
        tags : ``List[List[int]]``
            The predicted tags.
        loss : ``torch.FloatTensor``, optional
            A scalar loss to be optimised. Only computed if gold label ``tags`` are provided.
        """
        embedded_text_input = self.text_field_embedder(tokens)
        batch_size, sequence_length, _ = embedded_text_input.size()
        mask = util.get_text_field_mask(tokens)

        if self.dropout:
            embedded_text_input = self.dropout(embedded_text_input)

        if self.encoder:
            encoded_text = self.encoder(embedded_text_input, mask)
        else:
            encoded_text = embedded_text_input

        if self.dropout:
            encoded_text = self.dropout(encoded_text)

        if self.feedforward is not None:
            encoded_text = self.feedforward(encoded_text)

        logits = self.tag_projection_layer(encoded_text)

        # initial mask is unmasked
        batch_upos_constraint_mask = torch.ones_like(logits)
        # Use constraints only if use_upos_constraints is true and we're either
        # (1) in evaluate mode or (2) training with constraints.
        if self.use_upos_constraints and (not self.training or self.train_with_constraints):
            # List of length (batch_size,), where each inner list is a list of
            # the UPOS tags for the associated token sequence.
            batch_upos_tags = [instance_metadata["upos_tags"] for instance_metadata in metadata]

            # List of length (batch_size,), where each inner list is a list of
            # the lemmas for the associated token sequence.
            if self.use_lemma_constraints:
                batch_lemmas = [instance_metadata["lemmas"] for
                                instance_metadata in metadata]
            else:
                batch_lemmas = [([None] * len(instance_metadata["upos_tags"])) for
                                instance_metadata in metadata]

            # Get a (batch_size, max_sequence_length, num_tags) mask with "1" in
            # tags that are allowed for a given UPOS, and "0" for tags that are
            # disallowed for an even UPOS.
            batch_upos_constraint_mask = self.get_upos_constraint_mask(batch_upos_tags=batch_upos_tags,
                                                                       batch_lemmas=batch_lemmas)
        logits = util.replace_masked_values(logits,
                                            batch_upos_constraint_mask,
                                            -1e32)
        class_probabilities = F.softmax(logits, dim=-1).view(
                [batch_size, sequence_length, self.num_tags]
        )

        output = {
                "mask": mask,
                "logits": logits,
                "batch_upos_constraint_mask": batch_upos_constraint_mask,
                "class_probabilities": class_probabilities,
                "tokens": [instance_metadata["tokens"] for instance_metadata in metadata]
        }

        if self.use_upos_constraints and (not self.training or self.train_with_constraints):
            output["constrained_logits"] = logits
            output["upos_tags"] = batch_upos_tags

        if tags is not None:
            # Add gold tags if they exist
            output["gold_tags"] = tags
            loss = util.sequence_cross_entropy_with_logits(logits, tags, mask)
            output["loss"] = loss
            for metric in self.accuracy_metrics.values():
                metric(class_probabilities, tags, mask.float())
        return output

    @overrides
    def decode(self, output_dict: Dict[str, torch.Tensor]) -> Dict[str, torch.Tensor]:
        """

        ``output_dict["tags"]`` and ``output_dict["gold_tags"]`` are lists of lists of tag_ids,
        so we use an ugly nested list comprehension.
        """
        mask = output_dict.pop("mask")
        lengths = util.get_lengths_from_binary_sequence_mask(mask)
        # Do a position-wise argmax over the class probabilities to recover the tags.
        all_predictions = output_dict["class_probabilities"]
        all_predictions = all_predictions.cpu().data.numpy()
        if all_predictions.ndim == 3:
            predictions_list = [all_predictions[i] for i in range(all_predictions.shape[0])]
        else:
            predictions_list = [all_predictions]
        all_tags = []
        for predictions, length in zip(predictions_list, lengths):
            argmax_indices = numpy.argmax(predictions, axis=-1)
            tags = [
                    self.vocab.get_token_from_index(x, namespace=self.label_namespace)
                    for x in argmax_indices[:length]
            ]
            all_tags.append(tags)
        output_dict["tags"] = all_tags

        # Converts the tag ids to the actual tags.
        gold_tags = output_dict.pop("gold_tags", None)
        if tags is not None:
            # TODO (nfliu): figure out why this is sometimes a tensor and sometimes a list.
            if torch.is_tensor(gold_tags):
                gold_tags = gold_tags.cpu().detach().numpy()
            output_dict["gold_tags"] = [
                    [self.vocab.get_token_from_index(gold_tag, namespace=self.label_namespace)
                     for gold_tag in instance_gold_tags[:length]]
                    for instance_gold_tags, length in zip(gold_tags, lengths)]
        return output_dict

    @overrides
    def get_metrics(self, reset: bool = False) -> Dict[str, float]:
        metrics_to_return = {metric_name: metric.get_metric(reset) for
                             metric_name, metric in self.accuracy_metrics.items()}
        return metrics_to_return

    def get_upos_constraint_mask(self,
                                 batch_upos_tags: List[List[str]],
                                 batch_lemmas: List[List[str]]):
        """
        Given POS tags and lemmas for a batch, return a mask of shape
        (batch_size, max_sequence_length, num_tags) mask with "1" in
        tags that are allowed for a given UPOS, and "0" for tags that are
        disallowed for a given UPOS.

        Parameters
        ----------
        batch_upos_tags: ``List[List[str]]``, required
            UPOS tags for a batch.
        batch_lemmas: ``List[List[str]]``, required
            Lemmas for a batch.

        Returns
        -------
        ``Tensor``, shape (batch_size, max_sequence_length, num_tags)
            A mask over the logits, with 1 in positions where a tag is allowed
            for its UPOS and 0 in positions where a tag is allowed for its UPOS.
        """
        # TODO(nfliu): this is pretty inefficient, maybe there's someway to make it batched?
        # Shape: (batch_size, max_sequence_length, num_tags)
        upos_constraint_mask = torch.ones(len(batch_upos_tags),
                                          len(max(batch_upos_tags, key=len)),
                                          self.num_tags,
                                          device=next(self.tag_projection_layer.parameters()).device) * -1e32
        # Iterate over the batch
        for example_index, (example_upos_tags, example_lemmas) in enumerate(
                zip(batch_upos_tags, batch_lemmas)):
            # Shape of example_constraint_mask: (max_sequence_length, num_tags)
            # Iterate over the upos tags for the example
            example_constraint_mask = upos_constraint_mask[example_index]
            for timestep_index, (timestep_upos_tag, timestep_lemma) in enumerate(  # pylint: disable=unused-variable
                    zip(example_upos_tags, example_lemmas)):
                # Shape of timestep_constraint_mask: (num_tags,)
                upos_constraint = self._upos_to_label_mask[timestep_upos_tag]
                lemma_constraint = self._lemma_upos_to_label_mask.get((timestep_lemma, timestep_upos_tag),
                                                                      torch.zeros_like(upos_constraint))
                example_constraint_mask[timestep_index] = (upos_constraint.long() |
                                                           lemma_constraint.long()).float()
        return upos_constraint_mask

def get_lemma_allowed_lexcats():
    lemmas_to_constraints = {}
    # (POS, LEXCAT)
    lemmas_to_constraints["for"] = {"SCONJ": {"INF", "INF.P"}}
    lemmas_to_constraints["to"] = {"PART": {"INF", "INF.P"}}
    lemmas_to_constraints["be"] = {"AUX": {"V"}}
    lemmas_to_constraints["versus"] = {"ADP": {"CCONJ"}, "SCONJ": {"CCONJ"}}
    logger.info("Additionally allowed lexcats for each UPOS and each lemma")
    json_lemmas_to_constraints = {}
    for lemma in lemmas_to_constraints:
        json_lemmas_to_constraints[lemma] = dict()
        for upos in lemmas_to_constraints[lemma]:
            json_lemmas_to_constraints[lemma][upos] = sorted(list(lemmas_to_constraints[lemma][upos]))
    logger.info(json.dumps(json_lemmas_to_constraints, indent=2))
    return lemmas_to_constraints

def get_upos_allowed_lexcats(stronger_constraints=False):
    """
    stronger_constraints: bool (optional, default=False)
        If True, return mark a LEXCAT as allowed for a particular UPOS
        only if the LEXCAT is _always_a llowed for that UPOS. For instance,
        if this is True, then LEXCAT "AUX" will not be marked as allowed for
        upos "V", since it's only ok when the lemma is "be". If the argument
        is false, then LEXCAT "AUX" will be marked as allowed for upos "V".
    """
    if stronger_constraints:
        logger.info("Using UPOS constraints that are stronger than necessary "
                    "(probably because we are also using lemma constraints).")
    # pylint: disable=too-many-return-statements
    def is_allowed(upos, lexcat, stronger):
        if lexcat.endswith('!@'):
            return True
        if upos == lexcat:
            return True
        if (upos, lexcat) in {('NOUN', 'N'), ('PROPN', 'N'), ('VERB', 'V'),
                              ('ADP', 'P'), ('ADV', 'P'), ('SCONJ', 'P'),
                              ('ADP', 'DISC'), ('ADV', 'DISC'), ('SCONJ', 'DISC'),
                              ('PART', 'POSS')}:
            return True
        mismatch_ok = False
        if lexcat.startswith('INF'):
            # LC INF/INF.P and UPOS SCONJ are only ok if the lemma is "for".
            if upos == "SCONJ" and stronger is False:
                mismatch_ok = True
            # LC INF/INF.P and UPOS PART are only ok if the lemma is "to".
            if upos == "PART" and stronger is False:
                mismatch_ok = True
        # LC V and UPOS AUX are ok only if the lemma is "be".
        if upos == "AUX" and lexcat == "V" and stronger is False:
            mismatch_ok = True
        if upos == 'PRON':
            if lexcat in ('PRON', 'PRON.POSS'):
                mismatch_ok = True
        if lexcat == 'ADV':
            if upos in ('ADV', 'PART'):
                mismatch_ok = True
        # LC CCONJ and UPOS ADP are ok only if the lemma is "versus"
        if upos == 'ADP' and lexcat == 'CCONJ' and stronger is False:
            mismatch_ok = True
        return mismatch_ok

    allowed_combinations = {}
    for lexcat in ALL_LEXCATS:
        for universal_pos in ALL_UPOS:
            if is_allowed(universal_pos, lexcat, stronger_constraints):
                if universal_pos not in allowed_combinations:
                    allowed_combinations[universal_pos] = set()
                allowed_combinations[universal_pos].add(lexcat)
    logger.info("Allowed lexcats for each UPOS:")
    json_allowed_combinations = {upos: sorted(list(lexcats)) for
                                 upos, lexcats in allowed_combinations.items()}
    logger.info(json.dumps(json_allowed_combinations, indent=2))
    return allowed_combinations