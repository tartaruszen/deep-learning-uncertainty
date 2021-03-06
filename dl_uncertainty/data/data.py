import os
import secrets
import pickle
import base64
import collections
import re

import numpy as np
import torch.utils.data
from torch.utils.data.dataset import ConcatDataset
from functools import lru_cache
import itertools
from tqdm import tqdm, trange

# Batching


def tuple_collate(batch):  # malo brže
    return tuple(map(np.array, zip(*batch)))


from torch.utils.data.dataloader import default_collate


def default_collate_numpy(batch):
    elem_type = type(batch[0])
    if elem_type.__module__ == 'numpy' and elem_type.__name__ != 'str_' \
            and elem_type.__name__ != 'string_':
        elem = batch[0]
        #if elem_type.__name__ == 'ndarray':
        #    # array of string classes and object
        #    if re.search('[SaUO]', elem.dtype.str) is not None:
        #        raise TypeError(error_msg.format(elem.dtype))
        #    return np.stack([b for b in batch], 0)
        if elem.shape == ():  # scalars
            #py_type = float if elem.dtype.name.startswith('float') else int
            #return np.array(list(map(py_type, batch)))
            return np.array(batch)
        else:
            return np.stack(batch, 0)
    elif isinstance(batch[0], int):
        return np.array(batch)
    elif isinstance(batch[0], float):
        return np.array(batch)
    elif isinstance(batch[0], str):
        return batch
    elif isinstance(batch[0], collections.Mapping):
        return {
            key: default_collate_numpy([d[key] for d in batch])
            for key in batch[0]
        }
    elif isinstance(batch[0], collections.Sequence):
        transposed = zip(*batch)
        return [default_collate_numpy(samples) for samples in transposed]
    else:
        assert False


# Dataset


class Dataset(torch.utils.data.Dataset):

    def __init__(self, data, info=dict(), name=None):
        self.name = name or "Dataset" + secrets.token_hex(4)
        self.data = data
        self.info = info

    def __getitem__(self, idx):
        return self.data[idx]

    def __len__(self):
        return len(self.data)

    def __add__(self, other):
        return self.join(other)

    def __str__(self):
        return f"Dataset(name={self.name}, info={self.info})"

    def __repr__(self):
        return str(self)

    def identity(self):
        return IdDataset(self)

    def test(self, callback):
        return TestDataset(self, callback)

    def map(self,
            func=lambda x: x,
            component=None,
            func_name=None,
            new_info=None):
        if component is not None:
            f = func  # to avoid lambda capturing itself
            func = lambda x: tuple(
                    f(c) if i == component else c for i, c in enumerate(x))
        ds = MappedDataset(self, func, func_name)
        if new_info is not None:
            ds.info = new_info
        return ds

    def filter(self, func=lambda x: x, func_name=None, new_info=None):
        self._print("Filtering dataset...")
        indices = np.array([i for i, d in enumerate(tqdm(self)) if func(d)])
        subset_hash = hash(tuple(indices)) % 16**5
        func_name = f'_{func_name}' if func_name else ''
        ds = SubDataset(self, indices, f'-filter{func_name}_{subset_hash:x}')
        if new_info is not None:
            ds.info = new_info
        return ds

    def batch(batch_size, drop_last=True, collate_fn=None):
        return BatchesDataset(self, batch_size)

        assert drop_last, "Not implemented"
        assert False, "TODO"

    def unbatch():
        assert False, "TODO"

    def cache(self, max_cache_size=np.inf):
        # caches the dataset in RAM (partially or wholly)
        return CachedDataset(self, max_cache_size)

    def cache_lru(self, max_cache_size=1000):
        # caches the dataset in RAM (partially or wholly) keepeing only the last
        # used examples
        if max_cache_size > len(self):
            return self.cache()
        return LRUCachedDataset(self, max_cache_size)

    def cache_hdd(self, directory, chunk_size=100):
        # caches the whole dataset in RAM and on HDD as a single file
        return HDDCachedDataset(self, directory, chunk_size)

    def cache_hdd_only(self, directory):
        # caches the whole dataset on disk without keeping it in RAM
        return HDDOnlyCachedDataset(self, directory)

    def permute(self, seed=53):
        indices = np.random.RandomState(seed=seed).permutation(len(self))
        return SubDataset(self, indices, f"-permute{seed}")

    def subset(self, indices):
        return SubDataset(self, indices)

    def repeat(self, number_of_repeats):
        return RepeatedDataset(self, number_of_repeats)

    def split(self, proportion: float = None, position: int = None):
        assert position or proportion
        indices = np.arange(len(self))
        pos = position or round(proportion * len(self))
        dss = SubDataset(self, indices[:pos]), SubDataset(self, indices[pos:])
        dss[0].name += f"_0_to_{pos-1}"
        dss[1].name += f"_{pos}_to_end"
        return dss

    def join(self, other, info=None):
        if type(other) is not list:
            other = [other]
        datasets = [self] + other
        info = info or datasets[0].info
        name = f"concat[" + ",".join(x.name for x in datasets) + "]"
        return Dataset(ConcatDataset(datasets), info, name)

    def extract_feature(self, feature):
        return [d[feature] for d in self]

    def _print(self, *args, **kwargs):
        print(*args, f"({self.name})", **kwargs)


# Dataset wrappers and proxies


class IdDataset(Dataset):

    def __init__(self, dataset):
        self.dataset = dataset
        self.name = dataset.name + "-id"
        self.info = self.dataset.info

    def __getitem__(self, idx):
        return self.dataset[idx]

    def __len__(self):
        return len(self.dataset)


class TestDataset(Dataset):

    def __init__(self, dataset, callback):
        self.dataset = dataset
        self.name = dataset.name + "-id"
        self.info = self.dataset.info
        self.callback = callback

    def __getitem__(self, idx):
        self.callback(idx)
        return self.dataset[idx]

    def __len__(self):
        return len(self.dataset)


class MappedDataset(Dataset):

    def __init__(self, dataset, func=lambda x: x, func_name=None):
        func_name = func_name or 'map'
        self.name = dataset.name + f"-{func_name}"
        self.dataset = dataset
        self.info = self.dataset.info
        self._func = func

    def __getitem__(self, idx):
        return self._func(self.dataset[idx])

    def __len__(self):
        return len(self.dataset)


class CachedDataset(Dataset):

    def __init__(self, dataset, max_cache_size=np.inf):
        cache_size = min(len(dataset), max_cache_size)
        self.cache_all = cache_size == len(dataset)
        if self.cache_all:
            self.name = dataset.name + "-cache"
            self._print("Caching whole dataset...")
            self.data = [x for x in tqdm(dataset)]
        else:
            self.name = dataset.name + f"-cache_0_to_{cache_size-1}"
            self._print(
                f"Caching {cache_size}/{len(dataset)} of the dataset in RAM...")
            self.data = [dataset[i] for i in trange(cache_size)]
            self.dataset = dataset
        self.info = dataset.info

    def __getitem__(self, idx):
        cache_hit = self.cache_all or idx < len(self.data)
        return (self.data if cache_hit else self.dataset)[idx]

    def __len__(self):
        return len(self.data if self.cache_all else self.dataset)


class LRUCachedDataset(Dataset):

    def __init__(self, dataset, max_cache_size=1000):
        self.name = dataset.name + "-cache_lru"
        self.dataset = dataset
        self.info = self.dataset.info

        @lru_cache(maxsize=max_cache_size)
        def _cached_get(i):
            return self.dataset[i]

        self._cached_get = _cached_get

    def __getitem__(self, idx):
        return self._cached_get(idx)

    def __len__(self):
        return len(self.dataset)


class HDDCachedDataset(Dataset):

    def __init__(self, dataset, cache_dir, chunk_size=100):
        self.cache_dir = cache_dir
        self.name = dataset.name + "-cache_hdd"
        os.makedirs(cache_dir, exist_ok=True)
        cache_path = f"{cache_dir}/{self.name}.p"
        data = None
        if os.path.exists(cache_path):
            try:
                self._print("Loading dataset cache from HDD...")
                with open(cache_path, 'rb') as f:
                    n = (len(dataset) - 1) // chunk_size + 1  # ceil
                    chunks = [pickle.load(f) for i in trange(n)]
                    data = list(itertools.chain(*chunks))
            except Exception as ex:
                self._print(ex)
                self._print("Removing invalid HDD-cached dataset...")
                os.remove(cache_path)
        if data is None:
            self._print(f"Caching whole dataset in RAM...")
            data = [x for x in tqdm(dataset)]

            def get_chunks(data):
                chunk = []
                for x in data:
                    chunk.append(x)
                    if len(chunk) >= chunk_size:
                        yield chunk
                        chunk = []
                yield chunk

            self._print("Saving dataset cache to HDD...")
            with open(cache_path, 'wb') as f:
                # examples pickled in chunks because of memory constraints
                for x in tqdm(get_chunks(data)):
                    pickle.dump(x, f, protocol=4)
        self.data, self.info = data, dataset.info

    def __getitem__(self, idx):
        return self.data[idx]

    def __len__(self):
        return len(self.data)


class HDDOnlyCachedDataset(Dataset):

    def __init__(self, dataset, cache_dir):
        self.dataset = dataset
        self.info = dataset.info
        self.name = dataset.name + "-cache_hdd_only"
        self.cache_dir = f"{cache_dir}/{self.name}"
        os.makedirs(self.cache_dir, exist_ok=True)

    def __getitem__(self, idx):
        cache_path = f"{self.cache_dir}/{idx}.p"
        example = None
        if os.path.exists(cache_path):
            try:
                with open(cache_path, 'rb') as cache_file:
                    example = pickle.load(cache_file)
            except:
                os.remove(cache_path)
        if example is None:
            example = self.dataset[idx]
            with open(f"{cache_path}", 'wb') as cache_file:
                pickle.dump(example, cache_file, protocol=4)
        return example

    def __len__(self):
        return len(self.dataset)


class SubDataset(Dataset):

    def __init__(self, dataset, indices, name_addition='-sub'):
        self.name = dataset.name + name_addition
        self.dataset = dataset
        self.indices = indices
        self.info = self.dataset.info

    def __getitem__(self, idx):
        return self.dataset[self.indices[idx]]

    def __len__(self):
        return len(self.indices)


class RepeatedDataset(Dataset):

    def __init__(self, dataset, number_of_repeats):
        self.name = dataset.name + f"-repeat{number_of_repeats}"
        self.dataset = dataset
        self.length = len(self.dataset) * number_of_repeats
        self.number_of_repeats = number_of_repeats
        self.info = self.dataset.info

    def __getitem__(self, idx):
        if idx < 0 or idx >= self.length:
            raise IndexError(idx)
        return self.dataset[idx % len(self.dataset)]

    def __len__(self):
        return self.length


class BatchesDataset(Dataset):

    def __init__(self, dataset, batch_size):
        self.name = dataset.name + f"-batch{batch_size}"
        self.dataset = dataset
        self.length = len(self.dataset) // batch_size
        self.batch_size = batch_size
        if len(self.dataset) > self.length * batch_size:
            self.length += 1
            self.last_batch_size = self.length * batch_size - len(self.dataset)
        else:
            self.last_batch_size = self.batch_size
        self.info = self.dataset.info

    def __getitem__(self, idx):
        if idx < 0 or idx >= self.length:
            raise IndexError(idx)
        batch_size = self.last_batch_size if idx == self.length - 1 else self.batch_size
        i_start = idx * self.batch_size
        return [self.dataset[i] for i in range(i_start, i_start + batch_size)]

    def __len__(self):
        return self.length


# DataLoader


class DataLoader(torch.utils.data.DataLoader):

    def __init__(self,
                 dataset,
                 batch_size,
                 shuffle=False,
                 num_workers=0,
                 drop_last=False,
                 collate_fn=default_collate_numpy):
        super().__init__(
            dataset,
            batch_size=batch_size,
            shuffle=shuffle,
            num_workers=num_workers,
            drop_last=drop_last,
            collate_fn=collate_fn)