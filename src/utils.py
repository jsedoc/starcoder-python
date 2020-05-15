import pickle
import re
import gzip
import sys
import argparse
import json
import random
import logging
import warnings
import numpy
import torch
from data import Missing, Unknown


warnings.filterwarnings("ignore")


def compute_losses(entities, reconstructions, spec, field_models):
    """
    Gather and return all the field losses in a dictionary.
    """
    losses = {}
    for field_name, entity_values in entities.items():
        mask = entity_values > 0
        field_type = type(spec.field_object(field_name))
        losses[field_type] = losses.get(field_type, {})
        if field_name in reconstructions:
            reconstruction_values = reconstructions[field_name]
            losses[field_type][field_name] = field_models[field_type][2](reconstruction_values, entity_values)
    return losses


def tensorize(vals):
    if any([isinstance(v, list) for v in vals]):
        max_length = max([len(v) for v in vals if isinstance(v, list)])
        vals = [(v + ([Missing.value] * (max_length - len(v)))) if v != None else [Missing.value] * max_length for v in vals]
    else:
        vals = [(Missing.value if v == None else v) for v in vals]
    return torch.tensor(vals)


def stack_batch(components, spec):
    lengths = [len(x) for x, _ in components]
    entities = sum([x for x, _ in components], [])
    adjacencies = [x for _, x in components]
    full_adjacencies = {}
    start = 0
    for l, adjs in zip(lengths, adjacencies):
        for name, adj in adjs.items():
            full_adjacencies[name] = full_adjacencies.get(name, numpy.full((len(entities), len(entities)), False))
            full_adjacencies[name][start:start + l, start:start + l] = adj.todense()
        start += l
    field_names = spec.regular_field_names #set(sum([[k for k in e.keys()] for e in entities], []))
    full_entities = {k : [] for k in field_names}
    for entity in entities:
        for field_name in field_names:
            full_entities[field_name].append(entity.get(field_name, None))
    full_entities = {k : tensorize(v) for k, v in full_entities.items()}
    ne = len(entities)
    for k, v in full_adjacencies.items():
        a, b = v.shape
        assert(ne == a and ne == b)
    return (full_entities, {k : torch.tensor(v) for k, v in full_adjacencies.items()})


def split_batch(entities, adjacencies, count):
    ix = list(range(len(entities)))
    random.shuffle(ix)
    first_ix = ix[0:count]
    second_ix = ix[count:]
    first_entities = [entities[i] for i in first_ix]
    second_entities = [entities[i] for i in second_ix]
    first_adjacencies = {}
    second_adjacencies = {}
    for rel_type, adj in adjacencies.items():
        adjacencies[rel_type] = adj[first_ix, :][:, first_ix]
        adjacencies[rel_type] = adj[second_ix, :][:, second_ix]
    return ((first_entities, first_adjacencies), (second_entities, second_adjacencies))



#
# This has some unpleasantly-complicated logic for different ways of 
# handling components that don't fit into a batch:
#
#   "strict" means "never create an oversized batch"
#   "subselect" means "it's OK to split components over multiple batches"
#
# If strict=True and subselect=False, components larger than the
# batch size will never be seen, partial or otherwise.
#
def batchify(data, batch_size, strict=False, subselect=True):
    component_ids = range(data.num_components)
    _component_ids = [c for c in component_ids]
    random.shuffle(_component_ids)
    current_batch = []
    current_total = 0
    for component_id in _component_ids:
        entities, adjacencies = data.component(component_id)
        while len(entities) > 0:
            if len(entities) > batch_size and subselect == False:
                # component is larger than batch size and not subselecting
                if strict == False:
                    # if not strict, just yield it
                    yield(stack_batch([(entities, adjacencies)], data._spec))
                entities = []                    
            elif current_total + len(entities) > batch_size:
                # component + current is larger than batch size
                if subselect == True:
                    (sub_entities, sub_adjacencies), (entities, adjacencies) = split_batch(entities, 
                                                                                           adjacencies, 
                                                                                           batch_size - current_total)
                    current_batch.append((sub_entities, sub_adjacencies))
                    yield(stack_batch(current_batch, data._spec))
                    current_batch, current_total = [], 0
                else:
                    yield(stack_batch(current_batch, data._spec))
                    current_batch, current_total = [], 0
            else:
                # component + current not large enough yet
                current_batch.append((entities, adjacencies))
                current_total += len(entities)
                entities = []
    if len(current_batch) > 0:
        # final batch
        yield(stack_batch(current_batch, data._spec))


def run_over_components(model, field_models, optim, loss_policy, data, batch_size, gpu, train, subselect=False):
    old_mode = model.training
    model.train(train)
    loss_by_field = {}
    loss = 0.0
    for entities, adjacencies in batchify(data, batch_size, gpu, subselect=subselect):
        #print(entities["entity_type"].shape)
        batch_loss_by_field = {}
        if gpu:
            entities = {k : v.cuda() for k, v in entities.items()}
            adjacencies = {k : v.cuda() for k, v in adjacencies.items()}
        optim.zero_grad()
        reconstructions, bottlenecks = model(entities, adjacencies)
        for field_type, fields in compute_losses(entities, reconstructions, data._spec, field_models).items():
            for field_name, losses in fields.items():
                batch_loss_by_field[(field_name, field_type)] = losses
        batch_loss = loss_policy(batch_loss_by_field)
        loss += batch_loss
        if train:
            batch_loss.backward()
            optim.step()
        for k, v in batch_loss_by_field.items():
            loss_by_field[k] = loss_by_field.get(k, [])
            loss_by_field[k].append(v.clone().detach())
    model.train(old_mode)
    return (loss, loss_by_field)


def run_epoch(model, field_models, optimizer, loss_policy, train_data, dev_data, batch_size, gpu):
    model.train(True)
    train_loss, train_loss_by_field = run_over_components(model, 
                                                          field_models, 
                                                          optimizer, 
                                                          loss_policy,
                                                          train_data, 
                                                          #train_components, 
                                                          batch_size, 
                                                          gpu, 
                                                          True)
    model.train(False)
    dev_loss, dev_loss_by_field = run_over_components(model, 
                                                      field_models, 
                                                      optimizer, 
                                                      loss_policy,
                                                      dev_data, 
                                                      #dev_components, 
                                                      batch_size, 
                                                      gpu, 
                                                      False)
    return (train_loss.clone().detach().cpu(), 
            {k : [v.clone().detach().cpu() for v in vv] for k, vv in train_loss_by_field.items()},
            dev_loss.clone().detach().cpu(),
            {k : [v.clone().detach().cpu() for v in vv] for k, vv in dev_loss_by_field.items()})