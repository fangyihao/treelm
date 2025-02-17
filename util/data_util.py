import itertools
import json
import pickle
import os

import numpy as np
import torch
from tqdm import tqdm
from transformers import (OPENAI_GPT_PRETRAINED_CONFIG_ARCHIVE_MAP, BERT_PRETRAINED_CONFIG_ARCHIVE_MAP,
                          XLNET_PRETRAINED_CONFIG_ARCHIVE_MAP, ROBERTA_PRETRAINED_CONFIG_ARCHIVE_MAP, DEBERTA_V2_PRETRAINED_CONFIG_ARCHIVE_MAP)
from transformers import (OpenAIGPTTokenizer, BertTokenizer, XLNetTokenizer, RobertaTokenizer, MobileBertTokenizer, AutoTokenizer)
from util.tokenization import GLMTokenizer

try:
    from transformers import ALBERT_PRETRAINED_CONFIG_ARCHIVE_MAP
    from transformers import AlbertTokenizer
except:
    pass

from util import concept_net
from util import util
from torch import nn
import networkx as nx
from torch_geometric.utils.convert import from_networkx
from torch_geometric.data import Data
MODEL_CLASS_TO_NAME = {
    'gpt': list(OPENAI_GPT_PRETRAINED_CONFIG_ARCHIVE_MAP.keys()),
    'bert': list(BERT_PRETRAINED_CONFIG_ARCHIVE_MAP.keys()),
    'xlnet': list(XLNET_PRETRAINED_CONFIG_ARCHIVE_MAP.keys()),
    'roberta': list(ROBERTA_PRETRAINED_CONFIG_ARCHIVE_MAP.keys()),
    'deberta': list(DEBERTA_V2_PRETRAINED_CONFIG_ARCHIVE_MAP.keys()),
    'lstm': ['lstm'],
}
try:
    MODEL_CLASS_TO_NAME['albert'] =  list(ALBERT_PRETRAINED_CONFIG_ARCHIVE_MAP.keys())
except:
    pass

MODEL_NAME_TO_CLASS = {model_name: model_class for model_class, model_name_list in MODEL_CLASS_TO_NAME.items() for model_name in model_name_list}

#Add SapBERT configuration
model_name = 'cambridgeltl/SapBERT-from-PubMedBERT-fulltext'
MODEL_NAME_TO_CLASS[model_name] = 'bert'

#Add MobileBert configuration
model_name = 'google/mobilebert-uncased'
MODEL_NAME_TO_CLASS[model_name] = 'mobilebert'

#Add AristoRoBERTa configuration
model_name = 'LIAMF-USP/aristo-roberta'
MODEL_NAME_TO_CLASS[model_name] = 'roberta'

#Add DeBERTa V3 configuration
model_name = 'microsoft/deberta-v3-large'
MODEL_NAME_TO_CLASS[model_name] = 'deberta'
model_name = 'microsoft/deberta-v3-base'
MODEL_NAME_TO_CLASS[model_name] = 'deberta'

model_name = 'glm-large'
MODEL_NAME_TO_CLASS[model_name] = 'glm'

GPT_SPECIAL_TOKENS = ['_start_', '_delimiter_', '_classify_']



class DataGenerator(object):
    """A data generator that batches the data and moves them to the corresponding devices."""
    def __init__(self, indexes, qids, labels,
                 node_data=[], adj_data=None):

        self.indexes = indexes
        self.qids = qids
        self.labels = labels
        self.node_data = node_data
        self.adj_data = adj_data

    def __len__(self):
        return self.indexes.size(0)

    def __iter__(self):

        n = self.indexes.size(0)
        for a in range(n):
            index = self.indexes[a]
            qid = self.qids[index]
            label = self.labels[index]

            node_data = [x[index] for x in self.node_data]

            edge_index_all, edge_type_all = self.adj_data
            #edge_index_all: nested list of shape (n_samples,), where each entry is tensor[2, E]
            #edge_type_all:  nested list of shape (n_samples,), where each entry is tensor[E, ]
            edge_index = edge_index_all[index]
            if len(edge_type_all) > 0:
                edge_type = edge_type_all[index]
            else:
                edge_type = None

            
            node_input_ids, node_attention_mask, node_token_type_ids, node_output_mask, node_context_mask, node_type_ids, node_scores = node_data
            
            data = Data(qid=qid, y=label, x = node_input_ids, edge_index = edge_index, 
                        node_type = node_type_ids, edge_type = edge_type, node_attention_mask = node_attention_mask, node_token_type_ids = node_token_type_ids, node_output_mask = node_output_mask,  node_scores = node_scores, node_context_mask=node_context_mask, num_nodes=node_input_ids.size(0))


            yield data


class DataLoader(object):

    def __init__(self, train_statement_path, train_adj_path,
                 dev_statement_path, dev_adj_path,
                 test_statement_path, test_adj_path,
                 model_name, max_node_num=200, max_seq_length=128,
                 is_inhouse=False, inhouse_train_qids_path=None,
                 subsample=1.0, n_train=-1, debug=False, cxt_node_connects_all=False, kg="cpnet"):
        super().__init__()
        #self.device0, self.device1 = device
        self.is_inhouse = is_inhouse
        self.debug = debug
        self.model_name = model_name
        self.max_node_num = max_node_num
        self.debug_sample_size = 32
        self.cxt_node_connects_all = cxt_node_connects_all

        self.model_type = MODEL_NAME_TO_CLASS[model_name]
        self.load_resources(kg)

        # Load training data
        print ('train_statement_path:', train_statement_path)
        self.train_qids, self.train_labels, self.train_encoder_data, train_concepts_by_sents_list = self.load_input_tensors(train_statement_path, max_seq_length)

        num_choice = self.train_encoder_data[0].size(1)
        self.num_choice = num_choice
        print ('num_choice:', num_choice)
        *self.train_decoder_data, self.train_adj_data = self.load_sparse_adj_data_with_contextnode(train_adj_path, max_node_num, train_concepts_by_sents_list)
        if not debug:
            assert all(len(self.train_qids) == len(self.train_adj_data[0]) == x.size(0) for x in [self.train_labels] + self.train_encoder_data + self.train_decoder_data)

        print("Finish loading training data.")

        # Load dev data
        self.dev_qids, self.dev_labels, self.dev_encoder_data, dev_concepts_by_sents_list = self.load_input_tensors(dev_statement_path, max_seq_length)
        *self.dev_decoder_data, self.dev_adj_data = self.load_sparse_adj_data_with_contextnode(dev_adj_path, max_node_num, dev_concepts_by_sents_list)
        if not debug:
            assert all(len(self.dev_qids) == len(self.dev_adj_data[0]) == x.size(0) for x in [self.dev_labels] + self.dev_encoder_data + self.dev_decoder_data)

        print("Finish loading dev data.")

        # Load test data
        if test_statement_path is not None:
            self.test_qids, self.test_labels, self.test_encoder_data, test_concepts_by_sents_list = self.load_input_tensors(test_statement_path, max_seq_length)
            *self.test_decoder_data, self.test_adj_data = self.load_sparse_adj_data_with_contextnode(test_adj_path, max_node_num, test_concepts_by_sents_list)
            if not debug:
                assert all(len(self.test_qids) == len(self.test_adj_data[0]) == x.size(0) for x in [self.test_labels] + self.test_encoder_data + self.test_decoder_data)

            print("Finish loading test data.")

        # If using inhouse split, we split the original training set into an inhouse training set and an inhouse test set.
        if self.is_inhouse:
            with open(inhouse_train_qids_path, 'r') as fin:
                inhouse_qids = set(line.strip() for line in fin)
            self.inhouse_train_indexes = torch.tensor([i for i, qid in enumerate(self.train_qids) if qid in inhouse_qids])
            self.inhouse_test_indexes = torch.tensor([i for i, qid in enumerate(self.train_qids) if qid not in inhouse_qids])

        # Optionally we can subsample the training set.
        assert 0. < subsample <= 1.
        if subsample < 1. or n_train >= 0:
            # n_train will override subsample if the former is not None
            if n_train == -1:
                n_train = int(self.train_size() * subsample)
            assert n_train > 0
            if self.is_inhouse:
                self.inhouse_train_indexes = self.inhouse_train_indexes[:n_train]
            else:
                self.train_qids = self.train_qids[:n_train]
                self.train_labels = self.train_labels[:n_train]
                self.train_encoder_data = [x[:n_train] for x in self.train_encoder_data]
                self.train_decoder_data = [x[:n_train] for x in self.train_decoder_data]
                #self.train_adj_data = self.train_adj_data[:n_train]
                assert all(len(self.train_qids) == x.size(0) for x in [self.train_labels] + self.train_encoder_data + self.train_decoder_data)
                #assert all(len(self.train_qids) == len(self.train_adj_data[0]) == x.size(0) for x in [self.train_labels] + self.train_encoder_data + self.train_decoder_data)
            assert self.train_size() == n_train
        print("train_size:", self.train_size())
        self.merge_graph(max_seq_length)
        print("Finish merging graph.")

    def train_size(self):
        return self.inhouse_train_indexes.size(0) if self.is_inhouse else len(self.train_qids)

    def dev_size(self):
        return len(self.dev_qids)

    def test_size(self):
        if self.is_inhouse:
            return self.inhouse_test_indexes.size(0)
        else:
            return len(self.test_qids) if hasattr(self, 'test_qids') else 0

    def train(self):
        if self.debug:
            train_indexes = torch.arange(self.debug_sample_size)
        elif self.is_inhouse:
            n_train = self.inhouse_train_indexes.size(0)
            train_indexes = self.inhouse_train_indexes[torch.randperm(n_train)]
        else:
            train_indexes = torch.randperm(len(self.train_qids))
        return DataGenerator(train_indexes, self.train_qids, self.train_labels, node_data=self.train_node_data, adj_data=self.train_adj_data)

    def train_eval(self):
        return DataGenerator(torch.arange(len(self.train_qids)), self.train_qids, self.train_labels, node_data=self.train_node_data, adj_data=self.train_adj_data)

    def dev(self):
        if self.debug:
            dev_indexes = torch.arange(self.debug_sample_size)
        else:
            dev_indexes = torch.arange(len(self.dev_qids))
        return DataGenerator(dev_indexes, self.dev_qids, self.dev_labels, node_data=self.dev_node_data, adj_data=self.dev_adj_data)

    def test(self):
        if self.debug:
            test_indexes = torch.arange(self.debug_sample_size)
        elif self.is_inhouse:
            test_indexes = self.inhouse_test_indexes
        else:
            test_indexes = torch.arange(len(self.test_qids))
        if self.is_inhouse:
            return DataGenerator(test_indexes, self.train_qids, self.train_labels, node_data=self.train_node_data, adj_data=self.train_adj_data)
        else:
            return DataGenerator(test_indexes, self.test_qids, self.test_labels, node_data=self.test_node_data, adj_data=self.test_adj_data)

    def load_resources(self, kg):
        # Load the tokenizer
        try:
            tokenizer_class = {'bert': BertTokenizer, 'xlnet': XLNetTokenizer, 'roberta': RobertaTokenizer, 'albert': AlbertTokenizer, 'mobilebert': MobileBertTokenizer, 'glm': GLMTokenizer}.get(self.model_type)
        except:
            tokenizer_class = {'bert': BertTokenizer, 'xlnet': XLNetTokenizer, 'roberta': RobertaTokenizer, 'mobilebert': MobileBertTokenizer, 'glm': GLMTokenizer}.get(self.model_type)
        if tokenizer_class is None:
            tokenizer_class = AutoTokenizer
        tokenizer = tokenizer_class.from_pretrained(self.model_name)
        self.tokenizer = tokenizer

        if kg == "cpnet":
            # Load cpnet
            cpnet_vocab_path = "data/cpnet/concept.txt"
            with open(cpnet_vocab_path, "r", encoding="utf8") as fin:
                self.id2concept = [w.strip() for w in fin]
            self.concept2id = {w: i for i, w in enumerate(self.id2concept)}
            self.id2relation = concept_net.merged_relations
        elif kg == "ddb":
            cpnet_vocab_path = "data/ddb/vocab.txt"
            with open(cpnet_vocab_path, "r", encoding="utf8") as fin:
                self.id2concept = [w.strip() for w in fin]
            self.concept2id = {w: i for i, w in enumerate(self.id2concept)}
            self.id2relation = [
                'belongstothecategoryof',
                'isacategory',
                'maycause',
                'isasubtypeof',
                'isariskfactorof',
                'isassociatedwith',
                'maycontraindicate',
                'interactswith',
                'belongstothedrugfamilyof',
                'child-parent',
                'isavectorfor',
                'mabeallelicwith',
                'seealso',
                'isaningradientof',
                'mabeindicatedby'
            ]
        else:
            raise ValueError("Invalid value for kg.")

    def load_input_tensors(self, input_jsonl_path, max_seq_length):
        """Construct input tensors for the LM component of the model."""
        cache_path = input_jsonl_path + "-sl{}".format(max_seq_length) + (("-" + self.model_type) if self.model_type != "roberta" else "") + '.loaded_cache'
        use_cache = True

        if use_cache and not os.path.exists(cache_path):
            use_cache = False

        if use_cache:
            with open(cache_path, 'rb') as f:
                input_tensors = util.CPU_Unpickler(f).load()
        else:
            if self.model_type in ('lstm',):
                raise NotImplementedError
            elif self.model_type in ('gpt',):
                input_tensors = load_gpt_input_tensors(input_jsonl_path, max_seq_length)
            elif self.model_type in ('bert', 'xlnet', 'roberta', 'albert', 'mobilebert', 'deberta', 'glm'):
                input_tensors = load_bert_xlnet_roberta_input_tensors(input_jsonl_path, max_seq_length, self.debug, self.tokenizer, self.debug_sample_size)
            if not self.debug:
                util.save_pickle(input_tensors, cache_path)
        return input_tensors

    def load_sparse_adj_data_with_contextnode(self, adj_pk_path, max_node_num, concepts_by_sents_list):
        """Construct input tensors for the GNN component of the model."""
        print("Loading sparse adj data...")
        cache_path = adj_pk_path + "-nodenum{}".format(max_node_num) + ("-cntsall" if self.cxt_node_connects_all else "") + '.loaded_cache'
        use_cache = True

        if use_cache and not os.path.exists(cache_path):
            use_cache = False

        if use_cache:
            with open(cache_path, 'rb') as f:
                adj_lengths_ori, concept_ids, node_type_ids, node_scores, adj_lengths, edge_index, edge_type, half_n_rel, special_nodes_mask = util.CPU_Unpickler(f).load()
        else:
            # Set special nodes and links
            context_node = 0
            n_special_nodes = 1
            cxt2qlinked_rel = 0
            cxt2alinked_rel = 1
            half_n_rel = len(self.id2relation) + 2
            if self.cxt_node_connects_all:
                cxt2other_rel = half_n_rel
                half_n_rel += 1

            adj_concept_pairs = []
            with open(adj_pk_path, "rb") as in_file:
                try:
                    while True:
                        ex = pickle.load(in_file)
                        if type(ex) == dict:
                            adj_concept_pairs.append(ex)
                        elif type(ex) == list:
                            adj_concept_pairs.extend(ex)
                        else:
                            raise TypeError("Invalid type for ex.")
                except EOFError:
                    pass

            n_samples = len(adj_concept_pairs) #this is actually n_questions x n_choices
            edge_index, edge_type = [], []
            adj_lengths = torch.zeros((n_samples,), dtype=torch.long)
            concept_ids = torch.full((n_samples, max_node_num), 1, dtype=torch.long)
            node_type_ids = torch.full((n_samples, max_node_num), 2, dtype=torch.long) #default 2: "other node"
            node_scores = torch.zeros((n_samples, max_node_num, 1), dtype=torch.float)
            special_nodes_mask = torch.zeros(n_samples, max_node_num, dtype=torch.bool)

            adj_lengths_ori = adj_lengths.clone()
            if not concepts_by_sents_list:
                concepts_by_sents_list = itertools.repeat(None)
            for idx, (_data, cpts_by_sents) in tqdm(enumerate(zip(adj_concept_pairs, concepts_by_sents_list)), total=n_samples, desc='loading adj matrices'):
                if self.debug and idx >= self.debug_sample_size * self.num_choice:
                    break
                adj, concepts, qm, am, cid2score = _data['adj'], _data['concepts'], _data['qmask'], _data['amask'], _data['cid2score']
                #adj: e.g. <4233x249 (n_nodes*half_n_rels x n_nodes) sparse matrix of type '<class 'numpy.bool'>' with 2905 stored elements in COOrdinate format>
                #concepts: np.array(num_nodes, ), where entry is concept id
                #qm: np.array(num_nodes, ), where entry is True/False
                #am: np.array(num_nodes, ), where entry is True/False
                assert len(concepts) == len(set(concepts))
                qam = qm | am
                #sanity check: should be T,..,T,F,F,..F
                assert qam[0] == True
                F_start = False
                for TF in qam:
                    if TF == False:
                        F_start = True
                    else:
                        assert F_start == False

                assert n_special_nodes <= max_node_num
                special_nodes_mask[idx, :n_special_nodes] = 1
                num_concept = min(len(concepts) + n_special_nodes, max_node_num) #this is the final number of nodes including contextnode but excluding PAD
                adj_lengths_ori[idx] = len(concepts)
                adj_lengths[idx] = num_concept

                #Prepare nodes
                concepts = concepts[:num_concept - n_special_nodes]
                concept_ids[idx, n_special_nodes:num_concept] = torch.tensor(concepts + 1)  #To accomodate contextnode, original concept_ids incremented by 1
                concept_ids[idx, 0] = context_node #this is the "concept_id" for contextnode

                #Prepare node scores
                if cid2score is not None:
                    if -1 not in cid2score:
                        cid2score[-1] = 0
                    for _j_ in range(num_concept):
                        _cid = int(concept_ids[idx, _j_]) - 1 # Now context node is -1
                        node_scores[idx, _j_, 0] = torch.tensor(cid2score[_cid])

                #Prepare node types
                node_type_ids[idx, 0] = 3 # context node
                node_type_ids[idx, 1:n_special_nodes] = 4 # sent nodes
                node_type_ids[idx, n_special_nodes:num_concept][torch.tensor(qm, dtype=torch.bool)[:num_concept - n_special_nodes]] = 0
                node_type_ids[idx, n_special_nodes:num_concept][torch.tensor(am, dtype=torch.bool)[:num_concept - n_special_nodes]] = 1

                #Load adj
                ij = torch.tensor(adj.row, dtype=torch.int64) #(num_matrix_entries, ), where each entry is coordinate
                k = torch.tensor(adj.col, dtype=torch.int64)  #(num_matrix_entries, ), where each entry is coordinate
                n_node = adj.shape[1]
                assert len(self.id2relation) == adj.shape[0] // n_node
                i, j = ij // n_node, ij % n_node

                #Prepare edges
                i += 2; j += 1; k += 1  # **** increment coordinate by 1, rel_id by 2 ****
                extra_i, extra_j, extra_k = [], [], []
                for _coord, q_tf in enumerate(qm):
                    _new_coord = _coord + n_special_nodes
                    if _new_coord > num_concept:
                        break
                    if q_tf:
                        extra_i.append(cxt2qlinked_rel) #rel from contextnode to question concept
                        extra_j.append(0) #contextnode coordinate
                        extra_k.append(_new_coord) #question concept coordinate
                    elif self.cxt_node_connects_all:
                        extra_i.append(cxt2other_rel) #rel from contextnode to other concept
                        extra_j.append(0) #contextnode coordinate
                        extra_k.append(_new_coord) #other concept coordinate
                for _coord, a_tf in enumerate(am):
                    _new_coord = _coord + n_special_nodes
                    if _new_coord > num_concept:
                        break
                    if a_tf:
                        extra_i.append(cxt2alinked_rel) #rel from contextnode to answer concept
                        extra_j.append(0) #contextnode coordinate
                        extra_k.append(_new_coord) #answer concept coordinate
                    elif self.cxt_node_connects_all:
                        extra_i.append(cxt2other_rel) #rel from contextnode to other concept
                        extra_j.append(0) #contextnode coordinate
                        extra_k.append(_new_coord) #other concept coordinate

                # half_n_rel += 2 #should be 19 now
                if len(extra_i) > 0:
                    i = torch.cat([i, torch.tensor(extra_i)], dim=0)
                    j = torch.cat([j, torch.tensor(extra_j)], dim=0)
                    k = torch.cat([k, torch.tensor(extra_k)], dim=0)
                ########################

                mask = (j < max_node_num) & (k < max_node_num)
                i, j, k = i[mask], j[mask], k[mask]
                i, j, k = torch.cat((i, i + half_n_rel), 0), torch.cat((j, k), 0), torch.cat((k, j), 0)  # add inverse relations
                edge_index.append(torch.stack([j,k], dim=0)) #each entry is [2, E]
                edge_type.append(i) #each entry is [E, ]

            if not self.debug:
                with open(cache_path, 'wb') as f:
                    pickle.dump([adj_lengths_ori, concept_ids, node_type_ids, node_scores, adj_lengths, edge_index, edge_type, half_n_rel, special_nodes_mask], f)


        ori_adj_mean  = adj_lengths_ori.float().mean().item()
        ori_adj_sigma = np.sqrt(((adj_lengths_ori.float() - ori_adj_mean)**2).mean().item())
        print('| ori_adj_len: mu {:.2f} sigma {:.2f} | adj_len: {:.2f} |'.format(ori_adj_mean, ori_adj_sigma, adj_lengths.float().mean().item()) +
            ' prune_rate： {:.2f} |'.format((adj_lengths_ori > adj_lengths).float().mean().item()) +
            ' qc_num: {:.2f} | ac_num: {:.2f} |'.format((node_type_ids == 0).float().sum(1).mean().item(),
                                                        (node_type_ids == 1).float().sum(1).mean().item()))

        edge_index = list(map(list, zip(*(iter(edge_index),) * self.num_choice))) #list of size (n_questions, n_choices), where each entry is tensor[2, E] #this operation corresponds to .view(n_questions, n_choices)
        edge_type = list(map(list, zip(*(iter(edge_type),) * self.num_choice))) #list of size (n_questions, n_choices), where each entry is tensor[E, ]

        concept_ids, node_type_ids, node_scores, adj_lengths, special_nodes_mask = [x.view(-1, self.num_choice, *x.size()[1:]) for x in (concept_ids, node_type_ids, node_scores, adj_lengths, special_nodes_mask)]
        #concept_ids: (n_questions, num_choice, max_node_num)
        #node_type_ids: (n_questions, num_choice, max_node_num)
        #node_scores: (n_questions, num_choice, max_node_num)
        #adj_lengths: (n_questions,　num_choice)
        return concept_ids, node_type_ids, node_scores, adj_lengths, special_nodes_mask, (edge_index, edge_type) #, half_n_rel * 2 + 1


    def merge_graph(self, max_seq_length):
        #self.train_qids, self.train_labels, self.train_encoder_data
        #self.train_decoder_data, self.train_adj_data
        #concept_ids, node_type_ids, node_scores, adj_lengths, special_nodes_mask = self.train_decoder_data
        
        self.train_node_data, self.train_adj_data = _merge_graph(self.train_encoder_data, self.train_decoder_data, self.train_adj_data, self.id2concept, self.tokenizer, max_seq_length, self.max_node_num)
        self.dev_node_data, self.dev_adj_data = _merge_graph(self.dev_encoder_data, self.dev_decoder_data, self.dev_adj_data, self.id2concept, self.tokenizer, max_seq_length, self.max_node_num)
        self.test_node_data, self.test_adj_data = _merge_graph(self.test_encoder_data, self.test_decoder_data, self.test_adj_data, self.id2concept, self.tokenizer, max_seq_length, self.max_node_num)


def _merge_graph(context_node_data, kg_data, adj, id2concept, tokenizer, max_seq_length, max_node_num):     
    # concept_ids: (n_questions, num_choice, max_node_num)
    # context_node_input_ids, context_node_input_mask, context_node_segment_ids, context_node_output_mask = context_node_data

    concept_ids, node_type_ids, node_scores, adj_lengths, special_nodes_mask = kg_data

    # merge graphs of choices to a united one (there is a graph per choice originally)

    merged_edge_index = []
    merged_edge_type = []
    merged_concept_ids = []
    merged_node_type_ids = []
    merged_node_scores = []
    num_choices = concept_ids[0].size(0)
    #print("concept_ids:",len(concept_ids))
    #print("adj:",len(list(zip(adj[0], adj[1]))))
    for concept_ids_per_qid, node_type_ids_per_qid, node_scores_per_qid, adj_lengths_per_qid, adj_per_qid in zip(concept_ids, node_type_ids, node_scores, adj_lengths, zip(adj[0], adj[1])):
        G=nx.Graph()
        #print("concept_ids_per_qid:",len(concept_ids_per_qid))
        #print("adj_per_qid:",len(list(zip(adj_per_qid[0], adj_per_qid[1]))))
        for choice_id, (concept_ids_per_choice, node_type_ids_per_choice, node_scores_per_choice, adj_length, adj_per_choice) in enumerate(zip(concept_ids_per_qid, node_type_ids_per_qid, node_scores_per_qid, adj_lengths_per_qid, zip(adj_per_qid[0], adj_per_qid[1]))):
            #print("node_scores_per_choice:", node_scores_per_choice[:adj_length])
            H=nx.Graph()
            #concept_ids_per_choice = [concept_id + choice_id if concept_id == 0 else concept_id + (num_choices-1) for concept_id in concept_ids_per_choice]
            concept_ids_per_choice = [concept_id + choice_id if concept_id == 0 else (concept_id + (num_choices-1)) + choice_id*(len(id2concept)+num_choices) for concept_id in concept_ids_per_choice]
            for concept_id, type_id, score in zip(concept_ids_per_choice[:adj_length],node_type_ids_per_choice[:adj_length], node_scores_per_choice[:adj_length]):
                H.add_node(concept_id.item(), concept_id=concept_id, type_id=type_id, score=score)
            for edge_index, edge_type in zip(zip(adj_per_choice[0][0], adj_per_choice[0][1]), adj_per_choice[1]):
                H.add_edge(concept_ids_per_choice[edge_index[0]].item(), concept_ids_per_choice[edge_index[1]].item(), edge_type = edge_type) 
            #print("G.nodes:", G.nodes(data=True))
            #print("G.edges:", G.edges(data=True))
            #print("H.nodes:", H.nodes(data=True))
            #print("H.edges:", H.edges(data=True))
            G = nx.compose(G,H)  
            #print("composed G.nodes:", G.nodes(data=True))
            #print("composed G.edges:", G.edges(data=True))
        
        U = nx.Graph()
        U.add_nodes_from(sorted(G.nodes(data=True)))
        U.add_edges_from(G.edges(data=True)) 
        
        '''
        # remove low score nodes
        low_score_nodes = sorted(list(U.nodes(data=True))[num_choices:], key=lambda node: node[1]["score"], reverse=True)[max_node_num-num_choices:]
        low_score_nodes = [node[0] for node in low_score_nodes]
        #print("low_score_nodes:",low_score_nodes)
    
        #print("nodes before:", U.nodes())
        #print("edges before:", U.edges())
        for node in low_score_nodes:
            if U.degree(node) > 1:
                N = list(U.neighbors(node))
                #patch_edges = set(itertools.combinations(N,2)) - set(U.edges())
                #patch_edges = set(zip(N, N[1:]+N[0:1])) - set(U.edges())
                N_wo_context = list(set(N) - set(range(num_choices)))
                if len(N_wo_context) > 0:
                    pivot = sorted([(n,U.nodes(data=True)[n]["score"]) for n in N_wo_context], key=lambda t: t[1], reverse=True)[0][0]
                    patch_edges = set(zip([pivot] * (len(N)-1), list(set(N) - set([pivot])))) - set(U.edges())
                    for edge in patch_edges:
                        U.add_edge(*edge, edge_type = U[node][edge[1]]["edge_type"])
            U.remove_node(node)     
        #print("nodes after:", U.nodes())
        #print("edges after:", U.edges())
        '''
         
        pyg_graph = from_networkx(U) 
        print("pyg_graph:", pyg_graph)
        #print("pyg_graph.concept_id:", pyg_graph.concept_id)
        #print("pyg_graph.edge_index:", pyg_graph.edge_index)
        try:
            merged_edge_index.append(pyg_graph.edge_index)
        except AttributeError:
            print ("AttributeError: 'GlobalStorage' object has no attribute 'edge_index'")
        try:
            merged_edge_type.append(pyg_graph.edge_type)
        except AttributeError:
            print ("AttributeError: 'GlobalStorage' object has no attribute 'edge_type'")    
        merged_concept_ids.append(pyg_graph.concept_id)
        merged_node_type_ids.append(pyg_graph.type_id)
        merged_node_scores.append(pyg_graph.score)
    
    id2encoded = {"input_ids":[], "input_mask":[],"segment_ids":[],"output_mask":[]}

    for cid, w in enumerate(id2concept):
        phrase = ' '.join(w.split('_'))
        encoded_input = tokenizer(phrase, padding="max_length", truncation=True, max_length=max_seq_length, return_token_type_ids=True, return_special_tokens_mask=True)
        
        id2encoded["input_ids"].append(encoded_input["input_ids"])
        id2encoded["input_mask"].append(encoded_input["attention_mask"])
        id2encoded["segment_ids"].append(encoded_input["token_type_ids"])
        id2encoded["output_mask"].append(encoded_input["special_tokens_mask"])

    merged_node_data = []
    for attr_index, key in enumerate(id2encoded):
        id2encoded_emb = nn.Embedding.from_pretrained(torch.FloatTensor(id2encoded[key]))
        kg_node_data = []
        for kg_index, kg_concept_ids in enumerate(merged_concept_ids):
            kg_node_encoded = id2encoded_emb((kg_concept_ids % (len(id2concept)+num_choices) - num_choices)[num_choices:])
            kg_node_data.append(torch.concat([context_node_data[attr_index][kg_index],kg_node_encoded], dim=0).long())
        merged_node_data.append(kg_node_data)
    
    node_context_mask = []
    for input_ids in merged_node_data[0]:
        node_context_mask_per_graph = torch.zeros(input_ids.size(0), dtype=torch.long) - 1
        for i in range(num_choices):
            node_context_mask_per_graph[i] = i
        node_context_mask.append(node_context_mask_per_graph)
    
    return (*merged_node_data,node_context_mask, merged_node_type_ids, merged_node_scores), (merged_edge_index, merged_edge_type)
        

def load_gpt_input_tensors(statement_jsonl_path, max_seq_length):
    def _truncate_seq_pair(tokens_a, tokens_b, max_length):
        """Truncates a sequence pair in place to the maximum length."""
        while True:
            total_length = len(tokens_a) + len(tokens_b)
            if total_length <= max_length:
                break
            if len(tokens_a) > len(tokens_b):
                tokens_a.pop()
            else:
                tokens_b.pop()

    def load_qa_dataset(dataset_path):
        """ Output a list of tuples(story, 1st continuation, 2nd continuation, label) """
        with open(dataset_path, "r", encoding="utf-8") as fin:
            output = []
            for line in fin:
                input_json = json.loads(line)
                label = ord(input_json.get("answerKey", "A")) - ord("A")
                output.append((input_json['id'], input_json["question"]["stem"], *[ending["text"] for ending in input_json["question"]["choices"]], label))
        return output

    def pre_process_datasets(encoded_datasets, num_choices, max_seq_length, start_token, delimiter_token, clf_token):
        """ Pre-process datasets containing lists of tuples(story, 1st continuation, 2nd continuation, label)

            To Transformer inputs of shape (n_batch, n_alternative, length) comprising for each batch, continuation:
            input_ids[batch, alternative, :] = [start_token] + story[:cap_length] + [delimiter_token] + cont1[:cap_length] + [clf_token]
        """
        tensor_datasets = []
        for dataset in encoded_datasets:
            n_batch = len(dataset)
            input_ids = np.zeros((n_batch, num_choices, max_seq_length), dtype=np.int64)
            mc_token_ids = np.zeros((n_batch, num_choices), dtype=np.int64)
            lm_labels = np.full((n_batch, num_choices, max_seq_length), fill_value=-1, dtype=np.int64)
            mc_labels = np.zeros((n_batch,), dtype=np.int64)
            for i, data, in enumerate(dataset):
                q, mc_label = data[0], data[-1]
                choices = data[1:-1]
                for j in range(len(choices)):
                    _truncate_seq_pair(q, choices[j], max_seq_length - 3)
                    qa = [start_token] + q + [delimiter_token] + choices[j] + [clf_token]
                    input_ids[i, j, :len(qa)] = qa
                    mc_token_ids[i, j] = len(qa) - 1
                    lm_labels[i, j, :len(qa) - 1] = qa[1:]
                mc_labels[i] = mc_label
            all_inputs = (input_ids, mc_token_ids, lm_labels, mc_labels)
            tensor_datasets.append(tuple(torch.tensor(t) for t in all_inputs))
        return tensor_datasets

    def tokenize_and_encode(tokenizer, obj):
        """ Tokenize and encode a nested object """
        if isinstance(obj, str):
            return tokenizer.convert_tokens_to_ids(tokenizer.tokenize(obj))
        elif isinstance(obj, int):
            return obj
        else:
            return list(tokenize_and_encode(tokenizer, o) for o in obj)

    tokenizer = OpenAIGPTTokenizer.from_pretrained('openai-gpt')
    tokenizer.add_tokens(GPT_SPECIAL_TOKENS)
    special_tokens_ids = tokenizer.convert_tokens_to_ids(GPT_SPECIAL_TOKENS)

    dataset = load_qa_dataset(statement_jsonl_path)
    examples_ids = [data[0] for data in dataset]
    dataset = [data[1:] for data in dataset]  # discard example ids
    num_choices = len(dataset[0]) - 2

    encoded_dataset = tokenize_and_encode(tokenizer, dataset)

    (input_ids, mc_token_ids, lm_labels, mc_labels), = pre_process_datasets([encoded_dataset], num_choices, max_seq_length, *special_tokens_ids)
    return examples_ids, mc_labels, input_ids, mc_token_ids, lm_labels


def load_bert_xlnet_roberta_input_tensors(statement_jsonl_path, max_seq_length, debug, tokenizer, debug_sample_size):
    class InputExample(object):

        def __init__(self, example_id, question, contexts, endings, label=None):
            self.example_id = example_id
            self.question = question
            self.contexts = contexts
            self.endings = endings
            self.label = label

    class InputFeatures(object):

        def __init__(self, example_id, choices_features, label):
            self.example_id = example_id
            self.choices_features = [
                {
                    'input_ids': input_ids,
                    'input_mask': input_mask,
                    'segment_ids': segment_ids,
                    'output_mask': output_mask,
                }
                for input_ids, input_mask, segment_ids, output_mask in choices_features
            ]
            self.label = label

    def read_examples(input_file):
        with open(input_file, "r", encoding="utf-8") as f:
            examples = []
            for line in f.readlines():
                json_dic = json.loads(line)
                label = ord(json_dic["answerKey"]) - ord("A") if 'answerKey' in json_dic else 0
                contexts = json_dic["question"]["stem"]
                if "para" in json_dic:
                    contexts = json_dic["para"] + " " + contexts
                if "fact1" in json_dic:
                    contexts = json_dic["fact1"] + " " + contexts
                examples.append(
                    InputExample(
                        example_id=json_dic["id"],
                        contexts=[contexts] * len(json_dic["question"]["choices"]),
                        question="",
                        endings=[ending["text"] for ending in json_dic["question"]["choices"]],
                        label=label
                    ))
        return examples

    def simple_convert_examples_to_features(examples, label_list, max_seq_length, tokenizer):
        """ Loads a data file into a list of `InputBatch`s
            `cls_token_at_end` define the location of the CLS token:
                - False (Default, BERT/XLM pattern): [CLS] + A + [SEP] + B + [SEP]
                - True (XLNet/GPT pattern): A + [SEP] + B + [SEP] + [CLS]
            `cls_token_segment_id` define the segment id associated to the CLS token (0 for BERT, 2 for XLNet)
        """
        label_map = {label: i for i, label in enumerate(label_list)}

        features = []
        concepts_by_sents_list = []
        for ex_index, example in tqdm(enumerate(examples), total=len(examples), desc="Converting examples to features"):
            if debug and ex_index >= debug_sample_size:
                break
            choices_features = []
            for ending_idx, (context, ending) in enumerate(zip(example.contexts, example.endings)):
                ans = example.question + " " + ending

                encoded_input = tokenizer(context, ans, padding="max_length", truncation=True, max_length=max_seq_length, return_token_type_ids=True, return_special_tokens_mask=True)
                input_ids = encoded_input["input_ids"]
                output_mask = encoded_input["special_tokens_mask"]
                input_mask = encoded_input["attention_mask"]
                segment_ids = encoded_input["token_type_ids"]

                assert len(input_ids) == max_seq_length
                #assert len(output_mask) == max_seq_length
                assert len(input_mask) == max_seq_length
                #assert len(segment_ids) == max_seq_length

                choices_features.append((input_ids, input_mask, segment_ids, output_mask))
            label = label_map[example.label]
            features.append(InputFeatures(example_id=example.example_id, choices_features=choices_features, label=label))

        return features, concepts_by_sents_list

    def select_field(features, field):
        return [[choice[field] for choice in feature.choices_features] for feature in features]

    def convert_features_to_tensors(features):
        all_input_ids = torch.tensor(select_field(features, 'input_ids'), dtype=torch.long)
        all_input_mask = torch.tensor(select_field(features, 'input_mask'), dtype=torch.long)
        all_segment_ids = torch.tensor(select_field(features, 'segment_ids'), dtype=torch.long)
        all_output_mask = torch.tensor(select_field(features, 'output_mask'), dtype=torch.bool)
        all_label = torch.tensor([f.label for f in features], dtype=torch.long)
        return all_input_ids, all_input_mask, all_segment_ids, all_output_mask, all_label

    examples = read_examples(statement_jsonl_path)
    features, concepts_by_sents_list = simple_convert_examples_to_features(examples, list(range(len(examples[0].endings))), max_seq_length, tokenizer)
    example_ids = [f.example_id for f in features]
    *data_tensors, all_label = convert_features_to_tensors(features)
    return example_ids, all_label, data_tensors, concepts_by_sents_list

