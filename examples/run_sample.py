import pathlib
import sys

sys.path.insert(0, str(pathlib.Path(__file__).resolve().parents[1] / 'src'))

from citation_lineage import process
from citation_lineage.io import load,publish
if __name__=='__main__':
 r=process(load('fixtures/input.json'));publish('run/sample.json',r);print(r['summary'])
