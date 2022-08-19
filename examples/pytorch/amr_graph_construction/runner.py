import argparse
import copy
import random
import time
import warnings
import numpy as np
import torch
import torch.optim as optim
import torch.nn.functional as F
from torch.utils.data import DataLoader
from tqdm import tqdm
from graph4nlp.pytorch.data.data import GraphData
from graph4nlp.pytorch.data.dataset import DataItem, Text2TreeDataItem

from graph4nlp.pytorch.datasets.mawps import MawpsDatasetForTree
from graph4nlp.pytorch.models.graph2tree import Graph2Tree
from graph4nlp.pytorch.modules.utils.config_utils import load_json_config
from graph4nlp.pytorch.modules.utils.tree_utils import Tree

from examples.pytorch.rgcn.rgcn import RGCN
from amr_graph_construction import AmrGraphConstruction

warnings.filterwarnings("ignore")

class AmrDataItem(DataItem):
    def __init__(self, input_text, output_text, output_tree, tokenizer, share_vocab=True):
        super(AmrDataItem, self).__init__(input_text, tokenizer)
        self.output_text = output_text
        self.share_vocab = share_vocab
        self.output_tree = output_tree

    def extract(self):
        """
        Returns
        -------
        Input tokens and output tokens
        """
        g: GraphData = self.graph

        input_tokens = []
        for i in range(g.get_node_num()):
            tokenized_token = self.tokenizer(g.node_attributes[i]["token"])
            input_tokens.extend(tokenized_token)
        
        for s in g.graph_attributes["sentence"]:
            input_tokens.extend(s.strip().split(" "))

        output_tokens = self.tokenizer(self.output_text)

        return input_tokens, output_tokens

class RGCNGraph2Tree(Graph2Tree):
    def _build_gnn_encoder(
        self,
        gnn,
        num_layers,
        input_size,
        hidden_size,
        output_size,
        direction_option,
        feats_dropout,
        gnn_heads=None,
        gnn_residual=True,
        gnn_attn_dropout=0.0,
        gnn_activation=F.relu,  # gat
        gnn_bias=True,
        gnn_allow_zero_in_degree=True,
        gnn_norm="both",
        gnn_weight=True,
        gnn_use_edge_weight=False,
        gnn_gcn_norm="both",  # gcn
        gnn_n_etypes=1,  # ggnn
        gnn_aggregator_type="lstm",  # graphsage
        **kwargs
    ):
        if gnn == "rgcn":
            self.gnn_encoder = RGCN(
                num_layers,
                input_size,
                hidden_size,
                output_size,
                num_rels=77,
                gpu=0,
            )
        else:
            raise NotImplementedError()

class Mawps:
    def __init__(self, opt=None):
        super(Mawps, self).__init__()
        self.opt = opt

        seed = self.opt["env_args"]["seed"]
        random.seed(seed)
        np.random.seed(seed)
        torch.manual_seed(seed)

        if self.opt["env_args"]["gpuid"] == -1:
            self.device = torch.device("cpu")
        else:
            self.device = torch.device("cuda:{}".format(self.opt["env_args"]["gpuid"]))

        self.use_copy = self.opt["model_args"]["decoder_args"]["rnn_decoder_share"]["use_copy"]
        self.use_share_vocab = self.opt["model_args"]["graph_construction_args"][
            "graph_construction_share"
        ]["share_vocab"]
        self.data_dir = self.opt["model_args"]["graph_construction_args"][
            "graph_construction_share"
        ]["root_dir"]

        self._build_dataloader()
        self._build_model()
        self._build_optimizer()

    def _build_dataloader(self):
        para_dic = {
            "root_dir": self.data_dir,
            "word_emb_size": self.opt["model_args"]["graph_initialization_args"]["input_size"],
            "topology_subdir": self.opt["model_args"]["graph_construction_args"][
                "graph_construction_share"
            ]["topology_subdir"],
            "edge_strategy": self.opt["model_args"]["graph_construction_args"][
                "graph_construction_private"
            ]["edge_strategy"],
            "graph_construction_name": self.opt["model_args"]["graph_construction_name"],
            "share_vocab": self.use_share_vocab,
            "enc_emb_size": self.opt["model_args"]["graph_initialization_args"]["input_size"],
            "dec_emb_size": self.opt["model_args"]["decoder_args"]["rnn_decoder_share"][
                "input_size"
            ],
            "dynamic_init_graph_name": self.opt["model_args"]["graph_construction_args"][
                "graph_construction_private"
            ].get("dynamic_init_graph_name", None),
            "min_word_vocab_freq": self.opt["preprocessing_args"]["min_freq"],
            "pretrained_word_emb_name": self.opt["preprocessing_args"]["pretrained_word_emb_name"],
            "nlp_processor_args": self.opt["model_args"]["graph_construction_args"][
                "graph_construction_share"
            ]["nlp_processor_args"],
            #"dataitem": Text2TreeDataItem,
            "dataitem": AmrDataItem,
            "topology_builder": AmrGraphConstruction,
        }

        dataset = MawpsDatasetForTree(**para_dic)

        self.train_data_loader = DataLoader(
            dataset.train,
            batch_size=self.opt["training_args"]["batch_size"],
            shuffle=True,
            num_workers=0,
            collate_fn=dataset.collate_fn,
        )
        self.test_data_loader = DataLoader(
            dataset.test, batch_size=1, shuffle=False, num_workers=0, collate_fn=dataset.collate_fn
        )
        self.valid_data_loader = DataLoader(
            dataset.val, batch_size=1, shuffle=False, num_workers=0, collate_fn=dataset.collate_fn
        )
        self.vocab_model = dataset.vocab_model
        self.src_vocab = self.vocab_model.in_word_vocab
        self.tgt_vocab = self.vocab_model.out_word_vocab
        #self.num_rel = len(dataset.edge_vocab)
        self.share_vocab = self.vocab_model.share_vocab if self.use_share_vocab else None

    def _build_model(self):
        """For encoder-decoder"""
        print(self.opt["model_args"]["graph_embedding_name"])
        if self.opt["model_args"]["graph_embedding_name"] == "rgcn":
            self.model = RGCNGraph2Tree.from_args(self.opt, vocab_model=self.vocab_model)
        else:
            self.model = Graph2Tree.from_args(self.opt, vocab_model=self.vocab_model)
        self.model.init(self.opt["training_args"]["init_weight"])
        self.model.to(self.device)

    def _build_optimizer(self):
        optim_state = {
            "learningRate": self.opt["training_args"]["learning_rate"],
            "weight_decay": self.opt["training_args"]["weight_decay"],
        }
        parameters = [p for p in self.model.parameters() if p.requires_grad]
        self.optimizer = optim.Adam(
            parameters, lr=optim_state["learningRate"], weight_decay=optim_state["weight_decay"]
        )

    def prepare_ext_vocab(self, batch_graph, src_vocab):
        oov_dict = copy.deepcopy(src_vocab)
        token_matrix = []
        for n in batch_graph.node_attributes:
            node_token = n["token"]
            if (n.get("type") is None or n.get("type") == 0) and oov_dict.get_symbol_idx(
                node_token
            ) == oov_dict.get_symbol_idx(oov_dict.unk_token):
                oov_dict.add_symbol(node_token)
            token_matrix.append(oov_dict.get_symbol_idx(node_token))
        batch_graph.node_features["token_id_oov"] = torch.tensor(token_matrix, dtype=torch.long).to(
            self.device
        )
        return oov_dict

    def train_epoch(self, epoch):
        loss_to_print = 0
        num_batch = len(self.train_data_loader)
        for _, data in tqdm(
            enumerate(self.train_data_loader),
            desc=f"Epoch {epoch:02d}",
            total=len(self.train_data_loader),
        ):
            batch_graph, batch_tree_list, batch_original_tree_list = (
                data["graph_data"],
                data["dec_tree_batch"],
                data["original_dec_tree_batch"],
            )
            batch_graph = batch_graph.to(self.device)
            self.optimizer.zero_grad()
            oov_dict = (
                self.prepare_ext_vocab(batch_graph, self.src_vocab) if self.use_copy else None
            )

            if self.use_copy:
                batch_tree_list_refined = []
                for item in batch_original_tree_list:
                    tgt_list = oov_dict.get_symbol_idx_for_list(item.strip().split())
                    tgt_tree = Tree.convert_to_tree(tgt_list, 0, len(tgt_list), oov_dict)
                    batch_tree_list_refined.append(tgt_tree)
            loss = self.model(
                batch_graph,
                batch_tree_list_refined if self.use_copy else batch_tree_list,
                oov_dict=oov_dict,
            )
            loss.backward()
            torch.nn.utils.clip_grad_value_(
                self.model.parameters(), self.opt["training_args"]["grad_clip"]
            )
            self.optimizer.step()
            loss_to_print += loss
        return loss_to_print / num_batch

    def train(self):
        best_acc = (-1, -1)
        best_model = None

        print("-------------\nStarting training.")
        for epoch in range(1, self.opt["training_args"]["max_epochs"] + 1):
            self.model.train()
            loss_to_print = self.train_epoch(epoch)
            print("epochs = {}, train_loss = {:.3f}".format(epoch, loss_to_print))
            if epoch > 10 and epoch % 5 == 0:
                test_acc = self.eval(self.model, mode="test")
                val_acc = self.eval(self.model, mode="val")
                if val_acc > best_acc[1]:
                    best_acc = (test_acc, val_acc)
                    best_model = self.model
        print("Best Acc: {:.3f}\n".format(best_acc[0]))
        best_model.save_checkpoint(
            self.opt["checkpoint_args"]["out_dir"], self.opt["checkpoint_args"]["checkpoint_name"]
        )
        return best_acc

    def eval(self, model, mode="val"):
        from mawps.src.evaluation import compute_tree_accuracy

        model.eval()
        reference_list = []
        candidate_list = []
        data_loader = self.test_data_loader if mode == "test" else self.valid_data_loader
        for data in tqdm(data_loader, desc="Eval: "):
            eval_input_graph, _, batch_original_tree_list = (
                data["graph_data"],
                data["dec_tree_batch"],
                data["original_dec_tree_batch"],
            )
            eval_input_graph = eval_input_graph.to(self.device)
            oov_dict = self.prepare_ext_vocab(eval_input_graph, self.src_vocab)

            if self.use_copy:
                assert len(batch_original_tree_list) == 1
                reference = oov_dict.get_symbol_idx_for_list(batch_original_tree_list[0].split())
                eval_vocab = oov_dict
            else:
                assert len(batch_original_tree_list) == 1
                reference = model.tgt_vocab.get_symbol_idx_for_list(
                    batch_original_tree_list[0].split()
                )
                eval_vocab = self.tgt_vocab

            candidate = model.translate(
                eval_input_graph,
                oov_dict=oov_dict,
                use_beam_search=True,
                beam_size=self.opt["inference_args"]["beam_size"],
            )
            candidate = [int(c) for c in candidate]
            num_left_paren = sum(1 for c in candidate if eval_vocab.idx2symbol[int(c)] == "(")
            num_right_paren = sum(1 for c in candidate if eval_vocab.idx2symbol[int(c)] == ")")
            diff = num_left_paren - num_right_paren
            if diff > 0:
                for _ in range(diff):
                    candidate.append(self.test_data_loader.tgt_vocab.symbol2idx[")"])
            elif diff < 0:
                candidate = candidate[:diff]
            # ref_str = convert_to_string(reference, eval_vocab)
            # cand_str = convert_to_string(candidate, eval_vocab)

            reference_list.append(reference)
            candidate_list.append(candidate)
        eval_acc = compute_tree_accuracy(candidate_list, reference_list, eval_vocab)
        print("{} accuracy = {:.3f}\n".format(mode, eval_acc))
        return eval_acc


################################################################################
# ArgParse and Helper Functions #
################################################################################
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
    import pprint

    print("**************** MODEL CONFIGURATION ****************")
    pprint.pprint(config)
    print("**************** MODEL CONFIGURATION ****************")


if __name__ == "__main__":
    torch.multiprocessing.set_start_method('spawn')
    import platform
    import multiprocessing

    if platform.system() == "Darwin":
        multiprocessing.set_start_method("spawn")

    cfg = get_args()
    config = load_json_config(cfg["json_config"])
    print_config(config)

    start = time.time()
    runner = Mawps(config)
    best_acc = runner.train()

    end = time.time()
    print("total time: {} minutes\n".format((end - start) / 60))
