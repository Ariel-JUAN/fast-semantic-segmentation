r"""PSPNet"""
from abc import abstractmethod
from functools import partial
import tensorflow as tf

import base_model as model

slim = tf.contrib.slim


class PSPNetArchitecture(model.FastSegmentationModel):
    """PSPNet Architecture definition."""

    def __init__(self,
                is_training,
                model_arg_scope,
                num_classes,
                feature_extractor,
                classification_loss,
                use_aux_loss=True,
                main_loss_weight=1,
                aux_loss_weight=0,
                add_summaries=True,
                scope=None):
        super(PSPNetArchitecture, self).__init__(num_classes=num_classes)
        self._is_training = is_training
        self._model_arg_scope = model_arg_scope
        self._num_classes = num_classes
        self._feature_extractor = feature_extractor
        self._classification_loss = classification_loss
        self._use_aux_loss = use_aux_loss
        self._main_loss_weight = main_loss_weight
        self._aux_loss_weight = aux_loss_weight
        self._add_summaries = add_summaries

    @property
    def shared_feature_extractor_scope(self):
        return 'SharedFeatureExtractor'

    @property
    def main_class_predictions_key(self):
        return 'class_predictions'

    @property
    def aux_predictions_key(self):
        return 'aux_predictions'

    @property
    def main_loss_key(self):
        return 'loss'

    @property
    def aux_loss_key(self):
        return 'aux_loss'

    def preprocess(self, inputs):
        if inputs.dtype is not tf.float32:
            raise ValueError('`preprocess` expects a tf.float32 tensor')

        with tf.name_scope('Preprocessor'):
            return self._feature_extractor.preprocess(inputs)

    def _resize_logits_to_labels(self, logits, labels):
        labels_shape = labels.get_shape().as_list()
        scaled_logits = tf.image.resize_bilinear(
            logits, labels_shape[1:3], align_corners=True)
        return scaled_logits

    def _resize_labels_to_logits(self, labels, logits):
        logits_shape = logits.get_shape().as_list()
        scaled_labels = tf.image.resize_nearest_neighbor(
                labels, logits_shape[1:3], align_corners=True)
        return scaled_labels

    def _dynamic_interpolation(self, features_to_upsample,
                               s_factor=1, z_factor=1):
        with tf.name_scope('Interp'):
            _, input_h, input_w, _ = features_to_upsample.get_shape().as_list()
            shrink_h = (input_h-1)*s_factor+1
            shrink_w = (input_w-1)*s_factor+1
            zoom_h = shrink_h + (shrink_h-1)*(z_factor-1)
            zoom_w = shrink_w + (shrink_w-1)*(z_factor-1)
            return tf.image.resize_bilinear(features_to_upsample,
                                            [int(zoom_h), int(zoom_w)],
                                            align_corners=True)

    def predict(self, preprocessed_inputs, scope=None):
        """Build main inference pass"""
        with slim.arg_scope(self._model_arg_scope):
            # Feature extraction from arbitrary extractor
            _, backbone_logits, act = self._extract_shared_features(
                 preprocessed_inputs,
                 scope=self.shared_feature_extractor_scope)
            # Branch specific layers
            final_logits = self._icnet_adapted_pspmodule(backbone_logits)
            # Class class_predictions
            with tf.variable_scope('Predictions'):
                predictions = slim.conv2d(final_logits, self._num_classes,
                                1, 1, activation_fn=None, normalizer_fn=None)
                if not self._is_training: # evaluation
                    predictions = self._dynamic_interpolation(
                                        predictions, z_factor=8)
            # Outputs with auxilarary loss for training
            prediction_dict = {
                self.main_class_predictions_key: predictions}
            if self._use_aux_loss:
                with tf.variable_scope('AuxPredictions'):
                    aux_act = act[
                        'SharedFeatureExtractor/resnet_v1_50/block3/unit_2/bottleneck_v1/conv3']
                    aux_preds= slim.conv2d(aux_act, self._num_classes,
                                1, 1, activation_fn=None, normalizer_fn=None)
                prediction_dict[self.aux_predictions_key] = aux_preds
            return prediction_dict

    def _extract_shared_features(self, preprocessed_inputs, scope):
        return self._feature_extractor.extract_features(
                        preprocessed_inputs, scope=scope)

    def _icnet_adapted_pspmodule(self, input_features):
        """PSP Adapted module for ICNet..."""
        with tf.variable_scope('FastPSPModule'):
            _, input_h, input_w, _ = input_features.get_shape()

            full_pool = slim.avg_pool2d(input_features, [input_h, input_w],
                                stride=(input_h, input_w))
            full_pool = tf.image.resize_bilinear(full_pool,
                                size=(input_h, input_w),
                                align_corners=True)
            half_pool = slim.avg_pool2d(input_features,
                                        [input_h/2, input_w/2],
                                stride=(input_h/2, input_w/2))
            half_pool = tf.image.resize_bilinear(half_pool,
                                size=(input_h, input_w),
                                align_corners=True)
            third_pool = slim.avg_pool2d(input_features,
                                        [input_h/3, input_w/3],
                                stride=(input_h/3, input_w/3))
            third_pool = tf.image.resize_bilinear(third_pool,
                                size=(input_h, input_w),
                                align_corners=True)
            forth_pool = slim.avg_pool2d(input_features,
                                        [input_h/6, input_w/6],
                                stride=(input_h/6, input_w/6))
            forth_pool = tf.image.resize_bilinear(forth_pool,
                                size=(input_h, input_w),
                                align_corners=True)
            branch_merge = tf.add_n([input_features, full_pool,
                                     half_pool, third_pool, forth_pool])
            output = slim.conv2d(branch_merge, 512, [1, 1],
                                 stride=1, normalizer_fn=slim.batch_norm)
            return output

    def loss(self, prediction_dict, scope=None):
        losses_dict = {}
        main_preds = prediction_dict[self.main_class_predictions_key]
        with tf.name_scope('SegmentationLoss'): # 1/8th labels
            main_scaled_labels = self._resize_labels_to_logits(
                self._groundtruth_labels, main_preds)
            main_loss = self._classification_loss(main_preds,
                                            main_scaled_labels)
            losses_dict[self.main_loss_key] = (main_loss * self._main_loss_weight)

        if self._use_aux_loss and self._is_training:
            aux_preds = prediction_dict[self.aux_predictions_key]
            with tf.name_scope('AuxLoss'): # 1/8th labels
                aux_scaled_labels = self._resize_labels_to_logits(
                    self._groundtruth_labels, aux_preds)
                first_aux_loss = self._classification_loss(aux_preds,
                                                        aux_scaled_labels)
                losses_dict[self.aux_loss_key] = (
                    self._aux_loss_weight * first_aux_loss)
        return losses_dict

    def restore_map(self,
                    fine_tune_checkpoint_type='segmentation'):
        """Restore variables for checkpoints correctly"""
        if fine_tune_checkpoint_type not in [
                    'segmentation', 'classification', 'segmentation-finetune']:
            raise ValueError('Not supported '
                             'fine_tune_checkpoint_type: {}'.format(
                             fine_tune_checkpoint_type))
        if fine_tune_checkpoint_type == 'classification':
            return self._feature_extractor.restore_from_classif_checkpoint_fn(
                self.shared_feature_extractor_scope)

        exclude_list = ['global_step']
        variables_to_restore = slim.get_variables_to_restore(
                                        exclude=exclude_list)
        if fine_tune_checkpoint_type == 'segmentation':
            variables_to_restore.append(slim.get_or_create_global_step())

        return variables_to_restore


class PSPNetFeatureExtractor(object):
    """ICNet Feature Extractor definition."""

    def __init__(self,
                 is_training,
                 features_stride,
                 batch_norm_trainable=False,
                 reuse_weights=None,
                 weight_decay=0.0):
        self._is_training = is_training
        self._features_stride = features_stride
        self._train_batch_norm = (batch_norm_trainable and is_training)
        self._reuse_weights = reuse_weights
        self._weight_decay = weight_decay

    @abstractmethod
    def preprocess(self, resized_inputs):
        pass

    def extract_features(self, preprocessed_inputs, scope=None):
        """Extracts half resolution features."""
        with tf.variable_scope(
                scope, values=[preprocessed_inputs], reuse=tf.AUTO_REUSE):
            return self._extract_features(preprocessed_inputs, scope)

    @abstractmethod
    def _extract_features(self, preprocessed_inputs, scope):
        pass

    def restore_from_classif_checkpoint_fn(self, scope_name):
        variables_to_restore = {}
        for variable in tf.global_variables():
            if variable.op.name.startswith(scope_name):
                var_name = variable.op.name.replace(scope_name + '/', '')
                variables_to_restore[var_name] = variable
        return variables_to_restore