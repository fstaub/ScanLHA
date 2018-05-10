# import lzma
import logging
from collections import ChainMap
import os
from numpy import linspace, prod
from concurrent.futures import ProcessPoolExecutor as Executor
from concurrent.futures import as_completed
from tqdm import tqdm
from math import * # noqa: E403
from itertools import product
from pandas.io.json import json_normalize
from pandas import HDFStore
import json
from .slha import genSLHA
from .runner import RUNNERS
from numpy.random import seed, uniform
from time import time

def substitute(param_dict):
    subst = { p : str(v).format_map(param_dict) for p,v in param_dict.items() }
    if param_dict == subst:
        return { p : eval(v) for p,v in subst.items() }
    else:
        return substitute(subst)

class Scan():
    def __init__(self, c, runner='SLHA'):
        self.config = c
        self.config['runner']['template'] = genSLHA(c['blocks'])
        self.getblocks = self.config.get('getblocks', [])
        self.runner = RUNNERS[runner](c['runner'])
        self.scanset = []
        scan = None
        for block in c['blocks']:
            for line in block['lines']:
                if 'scan' in line:
                    self.addScanRange(block['block'], line)
                    scan = True
                elif 'values' in line:
                    self.addScanValues(block['block'], line)
                    scan = True
        if not scan:
            logging.info("No scan parameters defined in config!")
            logging.info("Register a scan range with addScanRange(<block>, {'id': <para-id>, 'scan': [<start>,<end>,<stepsize>]})")
            logging.info("Register a list of scan values with  addScanValues(<block>,{'id': <para-id>, 'values': [1,3,6,...])")

    def addScanRange(self, block, line):
        if 'id' not in line:
            logging.error("No 'id' set for parameter.")
            return
        if 'scan' not in line or len(line['scan']) < 2:
            logging.error("No proper 'scan' option set for parameter %d." % line['id'])
            return
        dist = self.config.distribution.get(line['distribution'], linspace) if 'distribution' in line else linspace
        line['scan'] = [ eval(str(s)) for s in line['scan'] ]
        line.update({'values': dist(*line['scan'])})
        self.addScanValues(block, line)

    def addScanValues(self, block, line):
        if 'id' not in line:
            logging.error("No 'id' set for parameter.")
            return
        if 'values' not in line or len(line['values']) < 1:
            logging.error("No proper 'values' option set for paramete %d." % line['id'])
            return
        self.config.setLine(block, line)
        # update the slha template with new config
        self.config['runner']['template'] = genSLHA(self.config['blocks'])

    def build(self,num_workers=4):
        if not self.config.validate():
            return
        values = []
        for parameter,line in self.config.parameters.items():
            if 'values' in line:
                values.append([{str(parameter): num} for num in line['values']])
            if 'dependent' in line and 'value' in line:
                valuse.append([line['parameter'], line['value']])
        self.numparas = prod([len(v) for v in values])
        logging.info('Build all %d parameter points.' % self.numparas)
        self.scanset = [ substitute(dict(ChainMap(*s))) for s in product(*values) ]
        if self.scanset:
            return self.numparas
        return

    def _run(self, dataset):
        # this is still buggy: https://github.com/tqdm/tqdm/issues/510
        # return [ self.spheno.run(d) for d in tqdm(dataset) ]
        return [ self.runner.run(d) for d in dataset ]

    def submit(self,w=None):
        w = os.cpu_count() if not w else w
        if not self.scanset:
            self.build()
        if w != 1:
            chunksize = min(int(self.numparas/w),1000)
            chunks = range(0, self.numparas, chunksize)
            logging.info('Running on host {}.'.format(os.getenv('HOSTNAME')))
            logging.info('Splitting dataset into %d chunks.' % len(chunks))
            logging.info('Will work on %d chunks in parallel.' % w)
            with Executor(w) as executor:
                futures = [ executor.submit(self._run, self.scanset[i:i+chunksize]) for i in chunks ]
                progresser = tqdm(as_completed(futures), total=len(chunks), unit = 'chunk')
                self.results = [ k for r in progresser for k in r.result() if k ]
        else:
            self.results = [ self.runner.run(d) for d in tqdm(self.scanset) ]
        self.results = json_normalize(json.loads(json.dumps(self.results)))

    def save(self, filename='store.hdf', path='results'):
        print('Saving to {} ({})'.format(filename,path))
        if path == 'config':
            logging.error('Cant use "config" as path, using "config2" instead.i')
            path = "config2"
        store = HDFStore(filename)
        store[path] = self.results
        store.get_storer(path).attrs.config = dict(self.config)
        store.close()

class RandomScan():
    def __init__(self, c, runner='SLHA'):
        self.config = c
        self.numparas = eval(str(c['runner']['numparas']))
        self.config['runner']['template'] = genSLHA(c['blocks'])
        self.getblocks = self.config.get('getblocks', [])
        self.runner = RUNNERS[runner](c['runner'])
        self.seed = round(time())
        seed(self.seed)
        self.randoms = { p : [eval(str(k)) for k in v['random']] for p,v in c.parameters.items() if 'random' in v }
        self.dependent = { p : v['value'] for p,v in c.parameters.items() if v.get('dependent',False) and 'value' in v }

    def _run(self, dataset):
        result = self.runner.run(dataset)
        try:
            if not all(map(eval, self.config['runner']['constraints'])):
                raise(ValueError)
            return result
        except (KeyError,ValueError):
            return
        except Exception as e:
            return {'log' : str(e)}

    def generate(self):
        dataset = { p : v for p,v in self.dependent.items() }
        [ dataset.update({ p : uniform(*v)}) for p,v in self.randoms.items() ]
        return substitute(dataset)

    def scan(self, numparas, pos=0):
        numresults = 0
        results = []
        with tqdm(total=numparas, unit='point', position=pos) as bar:
            while numresults < numparas:
                result = self._run(self.generate())
                if result:
                    results.append(result)
                    numresults += 1
                    bar.update(1)
        return results

    def submit(self,w=None):
        w = os.cpu_count() if not w else w
        logging.info('Running on host {}.'.format(os.getenv('HOSTNAME')))
        logging.info('Will work on %d threads in parallel.' % w)
        paras_per_thread = int(self.numparas/w)
        remainder = self.numparas % w
        numparas = [ paras_per_thread for p in range(w) ]
        numparas[-1] += remainder
        with Executor(w) as executor:
            futures = [ executor.submit(self.scan, j, pos=i) for i,j in enumerate(numparas) ]
            self.results = [ k for r in as_completed(futures) for k in r.result() if k ]
        self.results = json_normalize(json.loads(json.dumps(self.results)))

    def save(self, filename='store.hdf', path='results'):
        print('Saving to {} ({})'.format(filename,path))
        if path == 'config':
            logging.error('Cant use "config" as path, using "config2" instead.')
            path = "config2"
        store = HDFStore(filename)
        store[path] = self.results
        store.get_storer(path).attrs.config = dict(self.config)
        store.get_storer(path).attrs.seed = self.seed
        store.close()
