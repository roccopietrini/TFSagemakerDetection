#!/usr/bin/env python

import json
import os
import sys
import traceback
from shutil import copyfile

import tensorflow as tf

from utils import commandline_util


OBJECT_DETECTION_PATH = '/opt/ml/code/tensorflow-models/research/object_detection/'

# These are the paths to where SageMaker mounts interesting things in your container.
prefix = '/opt/ml/'
input_path = os.path.join(prefix, 'input/data')
output_path = os.path.join(prefix, 'output')
model_path = os.path.join(prefix, 'model')
param_path = os.path.join(prefix, 'input/config/hyperparameters.json')

# This algorithm has a single channel of input data called 'training'. Since we run in
# File mode, the input files are copied to the directory specified here.
channel_name = 'training'
training_path = os.path.join(input_path, channel_name)

tfrecord_label_path = os.path.join(training_path, 'label_map.pbtxt')
tfrecord_config_path = os.path.join(training_path, 'pipeline.config')
tfrecord_pretrained_checkpoint_path = os.path.join(input_path, 'checkpoint/')

training_script = os.path.join(OBJECT_DETECTION_PATH, 'model_main.py')
freezing_script = os.path.join(OBJECT_DETECTION_PATH, 'export_inference_graph.py')
lite_freezing_script = os.path.join(OBJECT_DETECTION_PATH, 'export_tflite_ssd_graph.py')
lite_quant_script = os.path.join(OBJECT_DETECTION_PATH, 'export_tflite_ssd_graph.py')


if __name__ == '__main__':
    try:
        print('Using Tensorflow version: {}'.format(tf.__version__))

        # Amazon SageMaker makes our specified hyperparameters available within the
        # /opt/ml/input/config/hyperparameters.json.
        with open(param_path, 'r') as tc:
            training_params = json.load(tc)

        print('Loaded training parameters: {}'.format(training_params))

        if 'num_steps' in training_params:
            num_steps_hyperparam = training_params['num_steps']
        else:
            num_steps_hyperparam = '100'

        if 'quantize' in training_params:
            quantize = str(training_params['quantize']).lower() == 'true'
        else:
            quantize = False

        if 'image_size' in training_params:
            image_size = training_params['image_size']
        else:
            image_size = '300'

        if 'inference_type' in training_params:
            inference_type = training_params['inference_type'].upper()
        else:
            inference_type = 'FLOAT'

        if 'model_dir' in training_params:
            model_path = training_params['model_dir']

        image_shape = ','.join(['1', image_size, image_size, '3'])

        print('Setting number of steps to {}'.format(num_steps_hyperparam))
        print('Setting quantization to {}'.format(quantize))
        print('Setting image shape to {}'.format(image_shape))
        print('Setting inference type to {}'.format(inference_type))

        file_list = os.listdir(tfrecord_pretrained_checkpoint_path)
        print("Extracted checkpoint files: {}".format(file_list))

        default_params = ['--model_dir', str(model_path),
                          '--pipeline_config_path', str(tfrecord_config_path),
                          '--num_train_steps', str(num_steps_hyperparam)]
        print('Starting the training...')
        commandline_util.run_python_script(training_script, default_params)

        # freeze and export the graph as .pb
        freeze_export_params = ['--input_type', str('image_tensor'),
                                '--pipeline_config_path', str(tfrecord_config_path),
                                '--trained_checkpoint_prefix', str(model_path + '/model.ckpt-' + num_steps_hyperparam),
                                '--output_directory', str(model_path + '/graph')]
        print('Exporting frozen graph...')
        commandline_util.run_python_script(freezing_script, freeze_export_params)

        if quantize:
            print('Quantizing frozen graph...')
            # freeze and export the graph for tflite processing
            lite_freeze_export_params = ['--pipeline_config_path', str(tfrecord_config_path),
                                         '--trained_checkpoint_prefix',
                                         str(model_path + '/model.ckpt-' + num_steps_hyperparam),
                                         '--output_directory', str(model_path + '/graph'),
                                         '--add_postprocessing_op', str('true')]
            print('Exporting Lite frozen graph...')
            commandline_util.run_python_script(lite_freezing_script, lite_freeze_export_params)

            graph_def_file = str(model_path + '/graph/tflite_graph.pb')
            output_tflite_graph = str(model_path + '/graph/tflite_quant_graph.tflite')
            input_arrays = ['normalized_input_image_tensor']
            output_arrays = ['TFLite_Detection_PostProcess', 'TFLite_Detection_PostProcess:1',
                             'TFLite_Detection_PostProcess:2', 'TFLite_Detection_PostProcess:3']
            input_shapes = {'normalized_input_image_tensor': [1, image_size, image_size, 3]}

            converter = tf.lite.TFLiteConverter.from_frozen_graph(graph_def_file, input_arrays,
                                                                          output_arrays, input_shapes)
            converter.allow_custom_ops = True

            if inference_type == 'QUANTIZED_UINT8':
                # quantize and export the tflite graph
                print('Quantizing Lite graph for QUANTIZED_UINT8 inference type...')

                converter.inference_type = tf.lite.constants.QUANTIZED_UINT8
                converter.quantized_input_stats = {'normalized_input_image_tensor': (128, 128)}
                tflite_model = converter.convert()

                open(output_tflite_graph, 'wb').write(tflite_model)

            elif inference_type == 'FLOAT':
                print('Quantizing Lite graph for FLOAT inference type...')

                converter.inference_type = tf.lite.constants.FLOAT
                converter.post_training_quantize = True
                tflite_model = converter.convert()

                open(output_tflite_graph, 'wb').write(tflite_model)

        copyfile(tfrecord_label_path, model_path + '/graph/label_map.pbtxt')
        copyfile(param_path, model_path + '/graph/hyperparameters.json')

        graph_files = os.listdir(model_path + '/graph')
        print('Successfully generated graphs: {}'.format(graph_files))

        # A zero exit code causes the job to be marked a Succeeded.
        sys.exit(0)
    except Exception as e:
        # Write out an error file. This will be returned as the failureReason in the
        # DescribeTrainingJob result.
        trc = traceback.format_exc()
        with open(os.path.join(output_path, 'failure'), 'w') as s:
            s.write('Exception during training: ' + str(e) + '\n' + trc)
        # Printing this causes the exception to be in the training job logs, as well.
        print('Exception during training: ' + str(e) + '\n' + trc, file=sys.stderr)
        # A non-zero exit code causes the training job to be marked as Failed.
        sys.exit(255)
