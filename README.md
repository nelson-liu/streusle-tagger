# Lexical Semantic Recognition Baselines

[![Build Status](https://travis-ci.com/nelson-liu/streusle-tagger.svg?branch=master)](https://travis-ci.com/nelson-liu/streusle-tagger)
[![codecov](https://codecov.io/gh/nelson-liu/streusle-tagger/branch/master/graph/badge.svg)](https://codecov.io/gh/nelson-liu/streusle-tagger)



## Installation

This project is being developed in Python 3.6.

[Conda](https://conda.io/) will set up a virtual environment with the exact
version of Python used for development along with all the dependencies needed to
run the code.

1.  [Download and install Conda](https://conda.io/docs/download.html).

2.  Change your directory to your clone of this repo.

    ```bash
    cd lexical-semantic-recognition-baselines
    ```

3.  Create a Conda environment with Python 3.6 .

    ```bash
    conda create -n lsr python=3.6
    ```

4.  Now activate the Conda environment. You will need to activate the Conda
    environment in each terminal in which you want to run code from this repo.

    ```bash
    conda activate lsr
    ```

5.  Install the required dependencies.

    ```bash
    pip install -r requirements.txt
    ```

You should now be able to test your installation with `py.test -v`.  Congratulations!

## Training a model

Training a model is as simple as:

```bash
allennlp train <train_config_path> \
    --include-package streusle_tagger \
    -s <path to save output to>
```

The training configs live in the [`./training_config` folder](./training_config).

For instance, to run a BERT model with all constraints during inference, but none during training, use
`streusle_bert_large_cased/streusle_bert_large_cased_all_constraints_no_train_constraints.jsonnet`.

### Modifying training configs

By changing the jsonnet training configs, you can control various aspects of your runs. For instance, by default,
all the STREUSLE 4.x training configs look for data at `./data/streusle/streusle.ud_{train|dev|test}.json`. To
train on other datasets, simply modify this path. You can also override it on the commandline with:

```
allennlp train <train_config_path> \
    --include-package streusle_tagger \
    -s <path to save output to> \
    --overrides '{"train_data_path": "<train_path>", "validation_data_path": "<validation_path>", "test_data_path": "<test_path>"}'
```

## Evaluating trained models

To evaluate trained models, we have scripts at `./scripts`, e.g. `./scripts/evaluate_on_streusle.sh`. Each of these
scripts loops through models saved at `./models`, and runs evaluation on them. Of course, if you have a particular
model you want to evaluate, you can always run these commands in the shell independent of the evaluation command.

For instance, to evaluate a STREUSLE 4.x tagger, we would use https://github.com/nelson-liu/streusle-tagger/blob/master/scripts/evaluate_on_streusle.sh .  On the shell:

1. We start by generating predictions from the model.
```
allennlp predict <path to saved model.tar.gz output by allennlp> <data_to_predict_on_path> \
    --silent \
    --output-file <model output and predictions path> \
    --silent \
    --include-package streusle_tagger \
    --use-dataset-reader \
    --predictor streusle-tagger \
    --cuda-device 0 \
    --batch-size 64
```

Change `--cuda-device` to `-1` if using CPU, and modify the batch size as you see fit.

Next, we evaluate with the official STREUSLE metric:

```
./scripts/streusle_eval/streuseval.sh \
    data/streusle/streusle.ud_dev.conllulex \
    <model output and predictions path>
```

This should write the metrics to the same folder as `<model output and predictions path>`.
