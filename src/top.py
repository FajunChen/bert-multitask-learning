import tensorflow as tf
from tensorflow.contrib import autograph

from tensor2tensor.utils import metrics

from .t2t_utils import get_t2t_metric_op

from bert import modeling


class TopLayer():
    def __init__(self, params):
        self.params = params

    def get_train(self):
        return self.loss

    def get_eval(self):
        return self.eval_metrics

    def get_predict(self):
        return self.prob

    def get_logit(self):
        return self.logits

    def __call__(self, features, hidden_feature, mode, problem_name):
        raise NotImplementedError


def gather_indexes(sequence_tensor, positions):
    """Gathers the vectors at the specific positions over a minibatch."""
    sequence_shape = modeling.get_shape_list(sequence_tensor, expected_rank=3)
    batch_size = sequence_shape[0]
    seq_length = sequence_shape[1]
    width = sequence_shape[2]

    flat_offsets = tf.reshape(
        tf.range(0, batch_size, dtype=tf.int32) * seq_length, [-1, 1])
    flat_positions = tf.reshape(positions + flat_offsets, [-1])
    flat_sequence_tensor = tf.reshape(sequence_tensor,
                                      [batch_size * seq_length, width])
    output_tensor = tf.gather(flat_sequence_tensor, flat_positions)
    return output_tensor


class SequenceLabel(TopLayer):

    def create_smooth_label(self, labels, num_classes):
        # since crf dose not take the smoothed label, consider the
        # 'hard' smoothing. That is, sample a tag based on smooth factor
        if self.params.label_smoothing > 0:

            true_labels = tf.stack(
                [labels]*int(num_classes/self.params.label_smoothing), axis=-1)
            single_label_set = tf.stack([tf.range(
                num_classes)]*self.params.max_seq_len, axis=0)
            batch_size_this_turn = tf.shape(true_labels)[0]
            label_set = tf.broadcast_to(
                input=single_label_set, shape=[batch_size_this_turn,
                                               single_label_set.shape.as_list()[
                                                   0],
                                               single_label_set.shape.as_list()[1]])
            sample_set = tf.concat([true_labels, label_set], axis=-1)

            dims = tf.shape(sample_set)
            sample_set = tf.reshape(sample_set, shape=[-1, dims[-1]])

            samples_index = tf.random_uniform(
                shape=[tf.shape(sample_set)[0], 1], minval=0, maxval=tf.shape(sample_set)[1], dtype=tf.int32)
            flat_offsets = tf.reshape(
                tf.range(0, tf.shape(sample_set)[0], dtype=tf.int32) * tf.shape(sample_set)[1], [-1, 1])
            flat_index = tf.reshape(samples_index+flat_offsets, [-1])
            sampled_label = tf.gather(
                tf.reshape(sample_set, [-1]), flat_index)
            sampled_label = tf.reshape(sampled_label, dims[:-1])
        return sampled_label

    def __call__(self, features, hidden_feature, mode, problem_name, mask=None):
        hidden_feature = hidden_feature['seq']
        if mode == tf.estimator.ModeKeys.TRAIN:
            hidden_feature = tf.nn.dropout(
                hidden_feature,
                keep_prob=self.params.dropout_keep_prob)

        if mask is None:
            num_classes = self.params.num_classes[problem_name]
        else:
            num_classes = mask.shape[0]

        output_layer = tf.layers.Dense(
            num_classes, activation=None,
            kernel_initializer=tf.orthogonal_initializer()
        )
        logits = output_layer(hidden_feature)
        self.logits = logits
        if mask is not None:
            logits = logits*mask

        # CRF transition param
        crf_transition_param = tf.get_variable(
            'crf_transition', shape=[num_classes, num_classes])

        # sequence_weight = tf.cast(features["input_mask"], tf.float32)
        seq_length = tf.reduce_sum(features["input_mask"], axis=-1)

        if mode == tf.estimator.ModeKeys.TRAIN:
            seq_labels = features['%s_label_ids' % problem_name]
            seq_labels = self.create_smooth_label(seq_labels, num_classes)
            with tf.variable_scope('CRF'):
                log_likelihood, _ = tf.contrib.crf.crf_log_likelihood(
                    logits, seq_labels, seq_length,
                    transition_params=crf_transition_param)
            loss_multiplier = tf.cast(
                features['%s_loss_multiplier' % problem_name], tf.float32)
            # multiply with loss multiplier to make some loss as zero
            seq_loss = tf.reduce_mean(-log_likelihood * loss_multiplier)
            tf.summary.scalar('%s_loss' % problem_name, seq_loss)
            self.loss = seq_loss
            return self.loss

        elif mode == tf.estimator.ModeKeys.EVAL:
            seq_labels = features['%s_label_ids' % problem_name]

            with tf.variable_scope('CRF'):

                log_likelihood, _ = tf.contrib.crf.crf_log_likelihood(
                    logits, seq_labels, seq_length,
                    transition_params=crf_transition_param)

            seq_loss = tf.reduce_mean(-log_likelihood)

            def metric_fn(label_ids, logits):
                predictions = tf.argmax(logits, axis=-1, output_type=tf.int32)
                prob = tf.nn.softmax(logits)
                accuracy = tf.metrics.accuracy(
                    label_ids, predictions, weights=features['input_mask'])
                acc_per_seq = get_t2t_metric_op(metrics.METRICS_FNS[
                    metrics.Metrics.ACC_PER_SEQ],
                    prob, features, label_ids)
                one_hot_labels = tf.one_hot(
                    label_ids, depth=num_classes)
                f1_score = tf.contrib.metrics.f1_score(
                    one_hot_labels, prob, weights=features['input_mask'])

                return {
                    "Accuracy": accuracy,
                    'Accuracy Per Sequence': acc_per_seq,
                    'F1 Score': f1_score
                }

            eval_metrics = (metric_fn(seq_labels, logits), seq_loss)
            self.eval_metrics = eval_metrics
            return self.eval_metrics
        elif mode == tf.estimator.ModeKeys.PREDICT:
            viterbi_sequence, viterbi_score = tf.contrib.crf.crf_decode(
                logits, crf_transition_param, seq_length)
            self.prob = viterbi_sequence
            return self.prob


class Classification(TopLayer):
    def create_loss(self, labels, logits,  num_classes):
        if self.params.label_smoothing > 0:
            one_hot_labels = tf.one_hot(labels, depth=num_classes)
            return tf.losses.softmax_cross_entropy(
                one_hot_labels, logits,
                label_smoothing=self.params.label_smoothing)
        else:
            return tf.losses.sparse_softmax_cross_entropy(labels, logits)

    def __call__(self, features, hidden_feature, mode, problem_name, mask=None):
        hidden_feature = hidden_feature['pooled']
        if mode == tf.estimator.ModeKeys.TRAIN:
            hidden_feature = tf.nn.dropout(
                hidden_feature,
                keep_prob=self.params.dropout_keep_prob)

        if mask is None:
            num_classes = self.params.num_classes[problem_name]
        else:
            num_classes = mask.shape[0]

        output_layer = tf.layers.Dense(
            num_classes, activation=None,
            kernel_initializer=tf.orthogonal_initializer()
        )
        logits = output_layer(hidden_feature)
        self.logits = logits
        if mask is not None:
            logits = logits*mask
        labels = features['%s_label_ids' % problem_name]
        if mode == tf.estimator.ModeKeys.TRAIN:
            batch_loss = self.create_loss(labels, logits, num_classes)
            loss_multiplier = tf.cast(
                features['%s_loss_multiplier' % problem_name], tf.float32)
            # multiply with loss multiplier to make some loss as zero
            loss = tf.reduce_mean(batch_loss*loss_multiplier)

            tf.summary.scalar('%s_loss' % problem_name, loss)
            self.loss = loss
            return self.loss
        elif mode == tf.estimator.ModeKeys.EVAL:
            batch_loss = self.create_loss(labels, logits, num_classes)
            loss_multiplier = tf.cast(
                features['%s_loss_multiplier' % problem_name], tf.float32)
            # multiply with loss multiplier to make some loss as zero
            loss = tf.reduce_mean(batch_loss*loss_multiplier)

            def metric_fn(label_ids, logits):
                predictions = tf.argmax(logits, axis=-1, output_type=tf.int32)
                prob = tf.nn.softmax(logits)
                accuracy = tf.metrics.accuracy(
                    label_ids, predictions)
                one_hot_labels = tf.one_hot(
                    label_ids, depth=num_classes)
                f1_score = tf.contrib.metrics.f1_score(
                    one_hot_labels, prob)

                return {
                    "Accuracy": accuracy,
                    'F1 Score': f1_score
                }
            eval_metrics = (metric_fn(labels, logits), loss)
            self.eval_metrics = eval_metrics
            return self.eval_metrics
        elif mode == tf.estimator.ModeKeys.PREDICT:
            prob = tf.nn.softmax(logits)
            self.prob = prob
            return self.prob


class MaskLM(TopLayer):
    def __call__(self, features, hidden_feature, mode, problem_name):
        """Get loss and log probs for the masked LM.

        DO NOT CHANGE THE VARAIBLE SCOPE.
        """
        seq_hidden_feature = hidden_feature['seq']
        positions = features['masked_lm_positions']
        input_tensor = gather_indexes(seq_hidden_feature, positions)
        output_weights = hidden_feature['embed_table']
        label_ids = features['masked_lm_ids']
        label_weights = features['masked_lm_weights']

        with tf.variable_scope("cls/predictions"):
            # We apply one more non-linear transformation before the output layer.
            # This matrix is not used after pre-training.
            with tf.variable_scope("transform"):
                input_tensor = tf.layers.dense(
                    input_tensor,
                    units=self.params.mask_lm_hidden_size,
                    activation=modeling.get_activation(
                        self.params.mask_lm_hidden_act),
                    kernel_initializer=modeling.create_initializer(
                        self.params.mask_lm_initializer_range))
                input_tensor = modeling.layer_norm(input_tensor)

            # The output weights are the same as the input embeddings, but there is
            # an output-only bias for each token.
            output_bias = tf.get_variable(
                "output_bias",
                shape=[self.params.vocab_size],
                initializer=tf.zeros_initializer())

            logits = tf.matmul(input_tensor, output_weights, transpose_b=True)
            logits = tf.nn.bias_add(logits, output_bias)
            self.logits = logits
            log_probs = tf.nn.log_softmax(logits, axis=-1)

            if mode == tf.estimator.ModeKeys.PREDICT:
                self.prob = log_probs
                return self.prob

            else:

                label_ids = tf.reshape(label_ids, [-1])
                label_weights = tf.reshape(label_weights, [-1])

                one_hot_labels = tf.one_hot(
                    label_ids, depth=self.params.vocab_size, dtype=tf.float32)

                # The `positions` tensor might be zero-padded (if the sequence is too
                # short to have the maximum number of predictions). The `label_weights`
                # tensor has a value of 1.0 for every real prediction and 0.0 for the
                # padding predictions.
                per_example_loss = - \
                    tf.reduce_sum(log_probs * one_hot_labels, axis=[-1])
                numerator = tf.reduce_sum(label_weights * per_example_loss)
                denominator = tf.reduce_sum(label_weights) + 1e-5
                loss = numerator / denominator

                if mode == tf.estimator.ModeKeys.TRAIN:
                    self.loss = loss
                    return self.loss

                else:
                    def metric_fn(masked_lm_example_loss, masked_lm_log_probs, masked_lm_ids,
                                  masked_lm_weights):
                        """Computes the loss and accuracy of the model."""
                        masked_lm_log_probs = tf.reshape(masked_lm_log_probs,
                                                         [-1, masked_lm_log_probs.shape[-1]])
                        masked_lm_predictions = tf.argmax(
                            masked_lm_log_probs, axis=-1, output_type=tf.int32)
                        masked_lm_example_loss = tf.reshape(
                            masked_lm_example_loss, [-1])
                        masked_lm_ids = tf.reshape(masked_lm_ids, [-1])
                        masked_lm_weights = tf.reshape(
                            masked_lm_weights, [-1])
                        masked_lm_accuracy = tf.metrics.accuracy(
                            labels=masked_lm_ids,
                            predictions=masked_lm_predictions,
                            weights=masked_lm_weights)
                        masked_lm_mean_loss = tf.metrics.mean(
                            values=masked_lm_example_loss, weights=masked_lm_weights)

                        return {
                            "masked_lm_accuracy": masked_lm_accuracy,
                            "masked_lm_loss": masked_lm_mean_loss,
                        }
                    eval_metrics = (metric_fn(
                        per_example_loss, log_probs, label_ids,
                        label_weights), loss)

                    self.eval_metrics = eval_metrics
                    return self.eval_metrics


class PreTrain(TopLayer):
    def __call__(self, features, hidden_feature, mode, problem_name):
        mask_lm_top = MaskLM(self.params)
        cls = Classification(self.params)
        mask_lm_top_result = mask_lm_top(
            features, hidden_feature, mode, problem_name)
        features['next_sentence_loss_multiplier'] = 1
        next_sentence_top_result = cls(
            features, hidden_feature, mode, 'next_sentence')
        if mode == tf.estimator.ModeKeys.TRAIN:
            self.loss = mask_lm_top_result+next_sentence_top_result
            return self.loss
        elif mode == tf.estimator.ModeKeys.EVAL:
            mask_lm_eval_dict, mask_lm_loss = mask_lm_top_result
            next_sentence_eval_dict, next_sentence_loss = next_sentence_top_result
            mask_lm_eval_dict.update(next_sentence_eval_dict)
            self.eval_metrics = (mask_lm_eval_dict,
                                 mask_lm_loss+next_sentence_loss)
            return self.eval_metrics
        elif mode == tf.estimator.ModeKeys.PREDICT:
            self.prob = mask_lm_top_result
            return self.prob


class LabelTransferHidden(TopLayer):

    def __call__(self, features, hidden_feature, mode):
        new_hidden_feature = {}
        seq_hidden_state = []
        pooled_hidden_state = []
        for problem_dict in self.params.run_problem_list:
            for problem in problem_dict:
                if problem in self.params.share_top:
                    top_name = self.params.share_top[problem]

                else:
                    top_name = problem

                top_scope_name = '%s_top' % top_name

                with tf.variable_scope(top_scope_name, reuse=tf.AUTO_REUSE):
                    if self.params.problem_type[problem] == 'seq_tag':
                        seq_tag = SequenceLabel(self.params)
                        seq_tag(features,
                                hidden_feature, mode, problem)

                        seq_hidden_state.append(seq_tag.get_logit())
                    elif self.params.problem_type[problem] == 'cls':
                        cls = Classification(self.params)

                        cls(features,
                            hidden_feature, mode, problem)
                        pooled_hidden_state.append(cls.get_logit())

        if len(seq_hidden_state) >= 2:
            new_hidden_feature['seq'] = tf.concat(seq_hidden_state, axis=-1)
        if len(pooled_hidden_state) >= 2:
            new_hidden_feature['pooled'] = tf.concat(
                pooled_hidden_state, axis=-1)
        hidden_feature.update(new_hidden_feature)

        return hidden_feature
