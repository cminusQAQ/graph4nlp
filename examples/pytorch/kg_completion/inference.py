import argparse
import numpy as np
import torch
import torch.backends.cudnn as cudnn

from graph4nlp.pytorch.data.dataset import KGCompletionDataItem, KGCompletionDataset
from graph4nlp.pytorch.datasets.kinship import KinshipDataset
from graph4nlp.pytorch.inference_wrapper.classifier_inference_wrapper import (
    ClassifierInferenceWrapper,
)
from graph4nlp.pytorch.modules.utils.config_utils import load_json_config

from main import KGC


def ranking_and_hits_this(cfg, model, dev_rank_batcher, vocab, name, kg_graph=None):
    print("")
    print("-" * 50)
    print(name)
    print("-" * 50)
    print("")
    hits_left = []
    hits_right = []
    hits = []
    ranks = []
    ranks_left = []
    ranks_right = []
    for _ in range(10):
        hits_left.append([])
        hits_right.append([])
        hits.append([])

    for i, str2var in enumerate(dev_rank_batcher):
        e1 = str2var["e1_tensor"]
        e2 = str2var["e2_tensor"]
        rel = str2var["rel_tensor"]
        rel_reverse = str2var["rel_eval_tensor"]
        e2_multi1 = str2var["e2_multi1"].float()
        e2_multi2 = str2var["e2_multi2"].float()
        if cfg["cuda"]:
            e1 = e1.to("cuda")
            e2 = e2.to("cuda")
            rel = rel.to("cuda")
            rel_reverse = rel_reverse.to("cuda")
            e2_multi1 = e2_multi1.to("cuda")
            e2_multi2 = e2_multi2.to("cuda")

        pred1 = model.forward(e1, rel, kg_graph)
        pred2 = model.forward(e2, rel_reverse, kg_graph)
        pred1, pred2 = pred1.data, pred2.data
        e1, e2 = e1.data, e2.data
        e2_multi1, e2_multi2 = e2_multi1.data, e2_multi2.data
        for i in range(e1.shape[0]):
            # these filters contain ALL labels
            filter1 = e2_multi1[i].long()
            filter2 = e2_multi2[i].long()

            # save the prediction that is relevant
            target_value1 = pred1[i, e2[i, 0].item()].item()
            target_value2 = pred2[i, e1[i, 0].item()].item()
            # zero all known cases (this are not interesting)
            # this corresponds to the filtered setting
            pred1[i][filter1] = 0.0
            pred2[i][filter2] = 0.0
            # write base the saved values
            pred1[i][e2[i]] = target_value1
            pred2[i][e1[i]] = target_value2

        # sort and rank
        max_values, argsort1 = torch.sort(pred1, 1, descending=True)
        max_values, argsort2 = torch.sort(pred2, 1, descending=True)

        argsort1 = argsort1.cpu().numpy()
        argsort2 = argsort2.cpu().numpy()
        for i in range(e1.shape[0]):
            # find the rank of the target entities
            rank1 = np.where(argsort1[i] == e2[i, 0].item())[0][0]
            rank2 = np.where(argsort2[i] == e1[i, 0].item())[0][0]
            # rank+1, since the lowest rank is rank 1 not rank 0
            ranks.append(rank1 + 1)
            ranks_left.append(rank1 + 1)
            ranks.append(rank2 + 1)
            ranks_right.append(rank2 + 1)

            # this could be done more elegantly, but here you go
            for hits_level in range(10):
                if rank1 <= hits_level:
                    hits[hits_level].append(1.0)
                    hits_left[hits_level].append(1.0)
                else:
                    hits[hits_level].append(0.0)
                    hits_left[hits_level].append(0.0)

                if rank2 <= hits_level:
                    hits[hits_level].append(1.0)
                    hits_right[hits_level].append(1.0)
                else:
                    hits[hits_level].append(0.0)
                    hits_right[hits_level].append(0.0)

        # dev_rank_batcher.state.loss = [0]

    for i in range(10):
        print("Hits left @{0}: {1}".format(i + 1, np.mean(hits_left[i])))
        print("Hits right @{0}: {1}".format(i + 1, np.mean(hits_right[i])))
        print("Hits @{0}: {1}".format(i + 1, np.mean(hits[i])))
    print("Mean rank left: {0}".format(np.mean(ranks_left)))
    print("Mean rank right: {0}".format(np.mean(ranks_right)))
    print("Mean rank: {0}".format(np.mean(ranks)))
    print("Mean reciprocal rank left: {0}".format(np.mean(1.0 / np.array(ranks_left))))
    print("Mean reciprocal rank right: {0}".format(np.mean(1.0 / np.array(ranks_right))))
    print("Mean reciprocal rank: {0}".format(np.mean(1.0 / np.array(ranks))))

    return np.mean(1.0 / np.array(ranks))


def main(cfg, model_path):
    np.random.seed(config["env_args"]["seed"])
    torch.manual_seed(config["env_args"]["seed"])

    if not config["env_args"]["no_cuda"] and torch.cuda.is_available():
        print("[ Using CUDA ]")
        config["env_args"]["device"] = torch.device(
            "cuda" if config["env_args"]["gpu"] < 0 else "cuda:%d" % config["env_args"]["gpu"]
        )
        cudnn.benchmark = True
        torch.cuda.manual_seed(config["env_args"]["seed"])
    else:
        config["env_args"]["device"] = torch.device("cpu")

    print("\n" + config["checkpoint_args"]["out_dir"])

    dataset = KinshipDataset(
        root_dir="examples/pytorch/kg_completion/data/{}".format(
            cfg["preprocessing_args"]["dataset"]
        ),
        topology_subdir="kgc",
    )

    num_entities = len(dataset.vocab_model.in_word_vocab)
    num_relations = len(dataset.vocab_model.out_word_vocab)

    graph_path = "examples/pytorch/kg_completion/data/{}/processed/kgc/" "KG_graph.pt".format(
        cfg["preprocessing_args"]["dataset"]
    )
    KG_graph = torch.load(graph_path).to(cfg["env_args"]["device"])
    model = KGC(cfg, num_entities, num_relations).to(cfg["env_args"]["device"])

    model_params = torch.load(model_path)
    model.load_state_dict(model_params)
    model.eval()

    model.graph_construction_name = None
    model.vocab_model = dataset.vocab_model

    inference_tool = ClassifierInferenceWrapper(
        cfg=cfg,
        model=model,
        dataset=KGCompletionDataset,
        data_item=KGCompletionDataItem,
        lower_case=True,
    )

    # for kinship
    raw_contents = [
        (
            '{"e1": "person84", "e2": "person85", "rel": "term21", '
            '"rel_eval": "term21_reverse", "e2_multi1": "person85",'
            '"e2_multi2": "person84 person55 person74 person57 '
            'person66 person96"}',
            KG_graph,
        )
    ]
    # for wn18rr
    # raw_contents = ['{"e1": "12400489", "e2": "12651821",
    #  "rel": "_hypernym", "rel_eval": "_hypernym_reverse",
    #  "e2_multi1": "12651821", "e2_multi2": "12333053 12300840 12332218
    #  12717644 12774641 12190869 11693981 12766043 12333771 12400924
    #  12644902 12628986 12629666 12745564 12641413 12651611 12333530
    #  12366675 12775717 12707781 11706761 12765846 12327528 12345280
    #  12640607 12648045 12370174 12400720 12400489 12625003 12771192
    #  12399132 12633638 12648196 12744387 12636224 12744850 12761284
    #  12373100 12667406 12638218 12742290 12745386 12743352"}']
    inference_tool.predict(
        raw_contents=raw_contents, batch_size=cfg["training_args"]["batch_size"], iskgc=True
    )


def get_args():
    parser = argparse.ArgumentParser()
    parser.add_argument(
        "-json_config",
        "--json_config",
        required=True,
        type=str,
        help="path to the json config file",
    )
    args = vars(parser.parse_args())
    return args


def print_config(config):
    print("**************** MODEL CONFIGURATION ****************")
    for key in sorted(config.keys()):
        val = config[key]
        keystr = "{}".format(key) + (" " * (24 - len(key)))
        print("{} -->  {}".format(keystr, val))
    print("**************** MODEL CONFIGURATION ****************")


if __name__ == "__main__":
    cfg = get_args()
    config = load_json_config(cfg["json_config"])
    print_config(config)

    model_name = "{0}_{1}_{2}_{3}".format(
        config["model_args"]["model"],
        config["model_args"]["graph_embedding_args"]["graph_embedding_share"]["direction_option"],
        config["model_args"]["input_drop"],
        config["model_args"]["hidden_drop"],
    )
    model_path = "examples/pytorch/kg_completion/saved_models/{0}_{1}.model".format(
        config["preprocessing_args"]["dataset"], model_name
    )

    main(config, model_path)
