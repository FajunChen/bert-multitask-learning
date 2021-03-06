import time
from collections import defaultdict
import os
import pickle
from shutil import copy2, SameFileError
import pytablewriter
import pandas as pd

import tensorflow as tf

from src.input_fn import train_eval_input_fn, predict_input_fn
from src.metrics import ner_evaluate
from src.model_fn import BertMultiTask
from src.params import Params
from src.utils import create_path
from src.estimator import Estimator
from src.ckpt_restore_hook import RestoreCheckpointHook


EXPERIMENTS_LIST = [
    {'problems': ['pkucws', 'WeiboNER',
                  'cityucws', 'msrcws',  'bosonner',
                  'CTBCWS',  'ascws', 'msraner', 'CTBPOS'],

     'additional_params': {},
     'name': 'baseline'},
    {'problems': ['WeiboNER', 'pkucws', 'CTBPOS',
                  'cityucws', 'msrcws',  'bosonner', 'msraner',
                  'CTBCWS',  'ascws'],

     'additional_params': {'label_smoothing': 0.1},
     'name': 'baseline_label_smooth'},

    {'problems': ['CWS|NER|POS'],

     'additional_params': {'crf': False},
     'name': 'multitask_label_transfer_first_train'},
    {'problems': ['CWS|NER|POS'],

     'additional_params': {'label_transfer': True,
                           'init_checkpoint': 'tmp/multitask_label_transfer_first_train/CWS_NER_POS_ckpt/',
                           'init_lr': 0.001,
                           'freeze_step': 999999},
     'name': 'multitask_label_transfer'},
    {'name': 'multitask_baseline',
        'problems': ['CWS|NER|POS'],
        'additional_params': {}
     },
    {
        'name': 'mix_data_baseline',
        'problems': ['NER', 'POS', 'CWS'],

        'additional_params': {}
    },
    {
        'problems': ['CWS|NER|POS'],

        'additional_params': {'augument_mask_lm': True},
        'name': 'multitask_aug_mask_lm'
    }
]


def train_problem(params, problem, gpu=4, base='baseline'):
    tf.keras.backend.clear_session()

    if not os.path.exists('tmp'):
        os.mkdir('tmp')

    base = os.path.join('tmp', base)
    params.assign_problem(problem, gpu=int(gpu), base_dir=base)

    create_path(params.ckpt_dir)

    tf.logging.info('Checkpoint dir: %s' % params.ckpt_dir)
    time.sleep(3)

    model = BertMultiTask(params=params)
    model_fn = model.get_model_fn(warm_start=False)

    dist_trategy = tf.contrib.distribute.MirroredStrategy(
        num_gpus=int(gpu),
        cross_tower_ops=tf.contrib.distribute.AllReduceCrossTowerOps(
            'nccl', num_packs=int(gpu)))

    run_config = tf.estimator.RunConfig(
        train_distribute=dist_trategy,
        eval_distribute=dist_trategy,
        log_step_count_steps=params.log_every_n_steps)

    # ws = make_warm_start_setting(params)

    estimator = Estimator(
        model_fn,
        model_dir=params.ckpt_dir,
        params=params,
        config=run_config)
    train_hook = RestoreCheckpointHook(params)

    def train_input_fn(): return train_eval_input_fn(params)
    estimator.train(
        train_input_fn, max_steps=params.train_steps, hooks=[train_hook])

    return estimator


def eval_single_problem(params, problem, label_encoder_path, estimator, gpu=4, base='baseline'):

    params.assign_problem(problem, gpu=int(gpu), base_dir=base)
    eval_dict = {}

    # copy label encoder
    try:
        copy2(label_encoder_path, os.path.join(
            params.ckpt_dir, '%s_label_encoder.pkl' % problem))
    except SameFileError:
        pass

    def input_fn(): return train_eval_input_fn(params, mode='eval')
    if 'ner' not in problem and 'NER' not in problem:
        eval_dict.update(estimator.evaluate(input_fn=input_fn))
    else:
        pred = estimator.predict(input_fn=input_fn)
        pred_list = defaultdict(list)
        for p in pred:
            for pro in p:
                pred_list[pro].append(p[pro])
        for pro in pred_list:
            if 'NER' in pro or 'ner' in pro:
                raw_ner_eval = ner_evaluate(
                    pro, pred_list[pro], params)
                rename_dict = {}
                rename_dict['%s_Accuracy' % pro] = raw_ner_eval['Acc']
                rename_dict['%s_F1 Score' % pro] = raw_ner_eval['F1']
                rename_dict['%s_Precision' % pro] = raw_ner_eval['Precision']
                rename_dict['%s_Recall' % pro] = raw_ner_eval['Recall']
                eval_dict.update(rename_dict)
    return eval_dict


def eval_problem(params, raw_problem, estiamtor, gpu=4, base='baseline'):
    eval_problem_list = []
    base = os.path.join('tmp', base)
    eval_label_encoder_list = []
    for sub_problem in raw_problem.split('|'):
        eval_problem_list.append([sub_problem])
        if sub_problem == 'CWS':
            eval_problem_list[-1] += ['ascws', 'msrcws', 'pkucws',
                                      'cityucws', 'CTBCWS']

        elif sub_problem == 'NER':
            eval_problem_list[-1] += ['WeiboNER', 'bosonner', 'msraner']
        elif sub_problem == 'POS':
            eval_problem_list[-1] += ['CTBPOS']

        eval_label_encoder_list.append(os.path.join(
            params.ckpt_dir, '%s_label_encoder.pkl' % sub_problem))

    final_eval_dict = {}
    for problem_list, label_encoder_path in zip(
            eval_problem_list, eval_label_encoder_list):
        for problem in problem_list:
            final_eval_dict.update(eval_single_problem(
                params,
                problem=problem,
                label_encoder_path=label_encoder_path,
                estimator=estiamtor,
                gpu=gpu,
                base=base))
    return final_eval_dict


def create_result_table(group_by='problem'):
    with open('tmp/results.pkl', 'rb') as f:
        result_dict = pickle.load(f)

    table_list = []

    if group_by == 'problem':
        problem_list = list(result_dict['mix_data_baseline'].keys())
        problem_list = set([p.split('_')[0] for p in problem_list if p.split('_')[
                           0] not in ['loss', 'global']])
        for problem in problem_list:
            writer = pytablewriter.MarkdownTableWriter()
            writer.table_name = problem
            problem_result_dict = {
                '%s_Accuracy' % problem: [],
                '%s_F1 Score' % problem: [],
                '%s_Precision' % problem: [],
                '%s_Recall' % problem: [],
                '%s_Accuracy Per Sequence' % problem: []
            }
            name = []
            for experiment_name, experiment_result in result_dict.items():
                name.append(experiment_name)
                for metric in problem_result_dict:
                    if metric in experiment_result:
                        problem_result_dict[metric].append(
                            experiment_result[metric])
                    else:
                        problem_result_dict[metric].append('-')

            problem_result_dict['experiment'] = name

            # put name in the first col
            df = pd.DataFrame(problem_result_dict)
            cols = df.columns.tolist()
            cols = cols[-1:] + cols[:-1]
            df = df[cols]
            writer.from_dataframe(df)

            table_list.append(writer.dumps())

    write_str = ''.join(table_list)
    with open('baseline.md', 'w', encoding='utf8') as f:
        f.writelines(write_str)


def main():
    gpu = 3
    params = Params()

    if os.path.exists('tmp/results.pkl'):
        with open('tmp/results.pkl', 'rb') as f:
            result_dict = pickle.load(f)
    else:
        result_dict = defaultdict(dict)
    for experiment_set in EXPERIMENTS_LIST:
        print('Running Problem set %s' % experiment_set['name'])
        params = Params()

        if experiment_set['additional_params']:
            for k, v in experiment_set['additional_params'].items():
                setattr(params, k, v)

        for problem in experiment_set['problems']:
            if '%s_Accuracy' % problem not in result_dict[experiment_set['name']]:
                estiamtor = train_problem(
                    params, problem, gpu, experiment_set['name'])
                eval_dict = eval_problem(
                    params, problem, estiamtor, gpu, base=experiment_set['name'])
                result_dict[experiment_set['name']].update(eval_dict)
                print(result_dict)
                pickle.dump(result_dict, open('tmp/results.pkl', 'wb'))

    print(result_dict)

    pickle.dump(result_dict, open('tmp/results.pkl', 'wb'))
    create_result_table()


if __name__ == '__main__':
    tf.logging.set_verbosity(tf.logging.DEBUG)
    main()
