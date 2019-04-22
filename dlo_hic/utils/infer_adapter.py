import io
import subprocess
import random
from itertools import cycle

from dlo_hic.utils.fastaio import read_fasta, FastaRec
from dlo_hic.utils.fastqio import read_fastq
from dlo_hic.utils.suffix_tree.STree import STree


N_FQ_REC = 200
N_BATCH = 20
BATCH_SIZE = 5
SEARCH_START_POS = 76
START_POS_THRESH = 5


def multiple_alignment(input_str):
    """ Perform mafft return result fasta records """
    p = subprocess.Popen(["mafft", "-"], stdout=subprocess.PIPE, stderr=subprocess.PIPE, stdin=subprocess.PIPE)
    out, err = p.communicate(input_str.encode('utf-8'))
    p.kill()
    str_io = io.StringIO(out.decode('utf-8'))
    fa_recs = [r for r in read_fasta(str_io)]
    return fa_recs


def get_fq_recs(path, n=N_FQ_REC):
    fq_recs = []
    for i, fq_rec in enumerate(read_fastq(path)):
        if i == n:
            break
        fq_recs.append(fq_rec)
    return fq_recs


def to_mafft_input(fq_recs, start_pos=SEARCH_START_POS):
    """ Convert fasta records to fasta string. for mafft input. """
    fa_recs = []
    for fq in fq_recs:
        name = fq.seqid
        seq = fq.seq
        seq = seq[start_pos-1:]
        fa = FastaRec(name, seq)
        fa_recs.append(fa)
    fa_str = "".join([str(i) for i in fa_recs])
    return fa_str


def make_batchs(fa_recs, n_batch=N_BATCH, batch_size=BATCH_SIZE):
    """ Generate multiple align result batch. """
    iter = cycle(fa_recs)
    for _ in range(n_batch):
        batch = []
        for _ in range(batch_size):
            batch.append(next(iter))
        yield batch


def find_lcs(recs):
    """ Find longest common subsequence of records. """
    st = STree([r.seq for r in recs])
    lcs = st.lcs()
    start_pos = [r.seq.find(lcs) for r in recs]
    start_pos = [p for p in start_pos if p >= 0]
    min_ = min(start_pos) if start_pos else float('inf')
    return lcs, min_


def infer_adapter_seq(fastq_path, n_fq_rec=N_FQ_REC, start_pos=SEARCH_START_POS,
                      n_batch=N_BATCH, batch_size=BATCH_SIZE, start_pos_thresh=START_POS_THRESH):
    fq_recs = get_fq_recs(fastq_path, n_fq_rec)
    fa_str = to_mafft_input(fq_recs, start_pos)
    align_recs = multiple_alignment(fa_str)
    candidates = []
    for recs in make_batchs(align_recs, n_batch, batch_size):
        lcs, start_pos = find_lcs(recs)
        if '-' not in lcs:
            candidates.append((lcs, start_pos))
    candidates.sort(key=lambda c: (c[1], -len(c[0])))
    if not candidates:
        return infer_adapter_seq(fastq_path, n_fq_rec, start_pos, n_batch, batch_size, start_pos_thresh)
    seq, pos = candidates[0]
    if (pos > start_pos_thresh) or (pos == 0):
        return infer_adapter_seq(fastq_path, n_fq_rec, start_pos, n_batch, batch_size, start_pos_thresh)
    else:
        return seq, pos
