
"""Trains a verifiable model on Mnist or CIFAR-10."""

from __future__ import absolute_import
from __future__ import division
from __future__ import print_function

import os

from absl import app
from absl import flags
from absl import logging
import interval_bound_propagation as ibp
import tensorflow.compat.v1 as tf


FLAGS = flags.FLAGS
flags.DEFINE_enum('dataset', 'mnist', ['mnist', 'cifar10'],
                  'Dataset (either "mnist" or "cifar10").')
flags.DEFINE_enum('model', 'tiny', ['tiny', 'small', 'medium', 'large'],
                  'Model size.')
flags.DEFINE_string('output_dir', '/tmp/ibp_model', 'Output directory.')

# Options.
flags.DEFINE_integer('steps', 60001, 'Number of steps in total.')
flags.DEFINE_integer('test_every_n', 2000,
                     'Number of steps between testing iterations.')
flags.DEFINE_integer('warmup_steps', 2000, 'Number of warm-up steps.')
flags.DEFINE_integer('rampup_steps', 10000, 'Number of ramp-up steps.')
flags.DEFINE_integer('batch_size', 200, 'Batch size.')
flags.DEFINE_float('epsilon', .3, 'Target epsilon.')
flags.DEFINE_float('epsilon_train', .33, 'Train epsilon.')
flags.DEFINE_string('learning_rate', '1e-3,1e-4@15000,1e-5@25000',
                    'Learning rate schedule of the form: '
                    'initial_learning_rate[,learning:steps]*. E.g., "1e-3" or '
                    '"1e-3,1e-4@15000,1e-5@25000".')
flags.DEFINE_float('nominal_xent_init', 1.,
                   'Initial weight for the nominal cross-entropy.')
flags.DEFINE_float('nominal_xent_final', .5,
                   'Final weight for the nominal cross-entropy.')
flags.DEFINE_float('verified_xent_init', 0.,
                   'Initial weight for the verified cross-entropy.')
flags.DEFINE_float('verified_xent_final', .5,
                   'Final weight for the verified cross-entropy.')
flags.DEFINE_float('crown_bound_init', 0.,
                   'Initial weight for mixing the CROWN bound with the IBP '
                   'bound in the verified cross-entropy.')
flags.DEFINE_float('crown_bound_final', 0.,
                   'Final weight for mixing the CROWN bound with the IBP '
                   'bound in the verified cross-entropy.')
flags.DEFINE_float('attack_xent_init', 0.,
                   'Initial weight for the attack cross-entropy.')
flags.DEFINE_float('attack_xent_final', 0.,
                   'Initial weight for the attack cross-entropy.')


def show_metrics(step_value, metric_values, loss_value=None):
  print('{}: {}nominal accuracy = {:.2f}%, '
        'verified = {:.2f}%, attack = {:.2f}%'.format(
            step_value,
            'loss = {}, '.format(loss_value) if loss_value is not None else '',
            metric_values.nominal_accuracy * 100.,
            metric_values.verified_accuracy * 100.,
            metric_values.attack_accuracy * 100.))


def layers(model_size):
  """Returns the layer specification for a given model name."""
  if model_size == 'tiny':
    return (
        ('linear', 100),
        ('activation', 'relu'))
  elif model_size == 'small':
    return (
        ('conv2d', (4, 4), 16, 'VALID', 2),
        ('activation', 'relu'),
        ('conv2d', (4, 4), 32, 'VALID', 1),
        ('activation', 'relu'),
        ('linear', 100),
        ('activation', 'relu'))
  elif model_size == 'medium':
    return (
        ('conv2d', (3, 3), 32, 'VALID', 1),
        ('activation', 'relu'),
        ('conv2d', (4, 4), 32, 'VALID', 2),
        ('activation', 'relu'),
        ('conv2d', (3, 3), 64, 'VALID', 1),
        ('activation', 'relu'),
        ('conv2d', (4, 4), 64, 'VALID', 2),
        ('activation', 'relu'),
        ('linear', 512),
        ('activation', 'relu'),
        ('linear', 512),
        ('activation', 'relu'))
  elif model_size == 'large':
    return (
        ('conv2d', (3, 3), 64, 'SAME', 1),
        ('activation', 'relu'),
        ('conv2d', (3, 3), 64, 'SAME', 1),
        ('activation', 'relu'),
        ('conv2d', (3, 3), 128, 'SAME', 2),
        ('activation', 'relu'),
        ('conv2d', (3, 3), 128, 'SAME', 1),
        ('activation', 'relu'),
        ('conv2d', (3, 3), 128, 'SAME', 1),
        ('activation', 'relu'),
        ('linear', 512),
        ('activation', 'relu'))
  else:
    raise ValueError('Unknown model: "{}"'.format(model_size))


def main(unused_args):
  logging.info('Training IBP on %s...', FLAGS.dataset.upper())
  step = tf.train.get_or_create_global_step()

  # Learning rate.
  learning_rate = ibp.parse_learning_rate(step, FLAGS.learning_rate)

  # Dataset.
  input_bounds = (0., 1.)
  num_classes = 10
  if FLAGS.dataset == 'mnist':
    data_train, data_test = tf.keras.datasets.mnist.load_data()
  else:
    assert FLAGS.dataset == 'cifar10', (
        'Unknown dataset "{}"'.format(FLAGS.dataset))
    data_train, data_test = tf.keras.datasets.cifar10.load_data()
    data_train = (data_train[0], data_train[1].flatten())
    data_test = (data_test[0], data_test[1].flatten())
  data = ibp.build_dataset(data_train, batch_size=FLAGS.batch_size,
                           sequential=False)
  if FLAGS.dataset == 'cifar10':
    data = data._replace(image=ibp.randomize(
        data.image, (32, 32, 3), expand_shape=(40, 40, 3),
        crop_shape=(32, 32, 3), vertical_flip=True))

  # Base predictor network.
  original_predictor = ibp.DNN(num_classes, layers(FLAGS.model))
  predictor = original_predictor
  if FLAGS.dataset == 'cifar10':
    mean = (0.4914, 0.4822, 0.4465)
    std = (0.2023, 0.1994, 0.2010)
    predictor = ibp.add_image_normalization(original_predictor, mean, std)
  if FLAGS.crown_bound_init > 0 or FLAGS.crown_bound_final > 0:
    logging.info('Using CROWN-IBP loss.')
    model_wrapper = ibp.crown.VerifiableModelWrapper
    loss_helper = ibp.crown.create_classification_losses
  else:
    model_wrapper = ibp.VerifiableModelWrapper
    loss_helper = ibp.create_classification_losses
  predictor = model_wrapper(predictor)

  # Training.
  train_losses, train_loss, _ = loss_helper(
      step,
      data.image,
      data.label,
      predictor,
      FLAGS.epsilon_train,
      loss_weights={
          'nominal': {
              'init': FLAGS.nominal_xent_init,
              'final': FLAGS.nominal_xent_final,
              'warmup': FLAGS.verified_xent_init + FLAGS.nominal_xent_init
          },
          'attack': {
              'init': FLAGS.attack_xent_init,
              'final': FLAGS.attack_xent_final
          },
          'verified': {
              'init': FLAGS.verified_xent_init,
              'final': FLAGS.verified_xent_final,
              'warmup': 0.
          },
          'crown_bound': {
              'init': FLAGS.crown_bound_init,
              'final': FLAGS.crown_bound_final,
              'warmup': 0.
          },
      },
      warmup_steps=FLAGS.warmup_steps,
      rampup_steps=FLAGS.rampup_steps,
      input_bounds=input_bounds)
  saver = tf.train.Saver(original_predictor.get_variables())
  optimizer = tf.train.AdamOptimizer(learning_rate)
  update_ops = tf.get_collection(tf.GraphKeys.UPDATE_OPS)
  with tf.control_dependencies(update_ops):
    train_op = optimizer.minimize(train_loss, step)

  # Test using while loop.
  def get_test_metrics(batch_size, attack_builder=ibp.UntargetedPGDAttack):
    """Returns the test metrics."""
    num_test_batches = len(data_test[0]) // batch_size
    assert len(data_test[0]) % batch_size == 0, (
        'Test data is not a multiple of batch size.')

    def cond(i, *unused_args):
      return i < num_test_batches

    def body(i, metrics):
      """Compute the sum of all metrics."""
      test_data = ibp.build_dataset(data_test, batch_size=batch_size,
                                    sequential=True)
      predictor(test_data.image, override=True, is_training=False)
      input_interval_bounds = ibp.IntervalBounds(
          tf.maximum(test_data.image - FLAGS.epsilon, input_bounds[0]),
          tf.minimum(test_data.image + FLAGS.epsilon, input_bounds[1]))
      predictor.propagate_bounds(input_interval_bounds)
      test_specification = ibp.ClassificationSpecification(
          test_data.label, num_classes)
      test_attack = attack_builder(predictor, test_specification, FLAGS.epsilon,
                                   input_bounds=input_bounds,
                                   optimizer_builder=ibp.UnrolledAdam)
      test_losses = ibp.Losses(predictor, test_specification, test_attack)
      test_losses(test_data.label)
      new_metrics = []
      for m, n in zip(metrics, test_losses.scalar_metrics):
        new_metrics.append(m + n)
      return i + 1, new_metrics

    total_count = tf.constant(0, dtype=tf.int32)
    total_metrics = [tf.constant(0, dtype=tf.float32)
                     for _ in range(len(ibp.ScalarMetrics._fields))]
    total_count, total_metrics = tf.while_loop(
        cond,
        body,
        loop_vars=[total_count, total_metrics],
        back_prop=False,
        parallel_iterations=1)
    total_count = tf.cast(total_count, tf.float32)
    test_metrics = []
    for m in total_metrics:
      test_metrics.append(m / total_count)
    return ibp.ScalarMetrics(*test_metrics)

  test_metrics = get_test_metrics(
      FLAGS.batch_size, ibp.UntargetedPGDAttack)
  summaries = []
  for f in test_metrics._fields:
    summaries.append(
        tf.summary.scalar(f, getattr(test_metrics, f)))
  test_summaries = tf.summary.merge(summaries)
  test_writer = tf.summary.FileWriter(os.path.join(FLAGS.output_dir, 'test'))

  # Run everything.
  tf_config = tf.ConfigProto()
  tf_config.gpu_options.allow_growth = True
  with tf.train.SingularMonitoredSession(config=tf_config) as sess:
    for _ in range(FLAGS.steps):
      iteration, loss_value, _ = sess.run(
          [step, train_losses.scalar_losses.nominal_cross_entropy, train_op])
      if iteration % FLAGS.test_every_n == 0:
        metric_values, summary = sess.run([test_metrics, test_summaries])
        test_writer.add_summary(summary, iteration)
        show_metrics(iteration, metric_values, loss_value=loss_value)
    saver.save(sess._tf_sess(),  # pylint: disable=protected-access
               os.path.join(FLAGS.output_dir, 'model'),
               global_step=FLAGS.steps - 1)


if __name__ == '__main__':
  app.run(main)
