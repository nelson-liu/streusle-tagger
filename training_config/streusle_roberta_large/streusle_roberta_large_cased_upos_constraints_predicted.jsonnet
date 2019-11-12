{
  "dataset_reader": {
    "type": "streusle_roberta",
    "roberta_type": "large",
    "max_seq_length": 128
  },
  "train_data_path": "https://raw.githubusercontent.com/nert-nlp/streusle/master/train/streusle.ud_train.json",
  "validation_data_path": "https://raw.githubusercontent.com/nert-nlp/streusle/master/dev/streusle.ud_dev.json",
  "test_data_path": "https://raw.githubusercontent.com/nert-nlp/streusle/master/test/streusle.ud_test.json",
  "model": {
    "type": "streusle_tagger_roberta",
    "roberta_type": "large",
    "use_upos_constraints": true,
    "use_lemma_constraints": false
  },
  "iterator": {
    "type": "basic",
    "batch_size": 64
  },
  "trainer": {
    "validation_metric": "+accuracy",
    "optimizer": {
        "type": "adam",
        "lr": 0.001
    },
    "num_serialized_models_to_keep": 1,
    "num_epochs": 75,
    "grad_norm": 5.0,
    "patience": 25,
    "cuda_device": 0
  }
}
