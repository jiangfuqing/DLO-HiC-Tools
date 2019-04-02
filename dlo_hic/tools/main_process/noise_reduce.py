import logging
import subprocess
from queue import Empty
import multiprocessing as mp
import gzip

import click
import numpy as np
import h5py

from dlo_hic.utils.parse_text import parse_line_bed6, parse_line_bedpe
from dlo_hic.utils.filetools import merge_tmp_files


TIME_OUT = 1
CHUNK_SIZE = 10000

log = logging.getLogger(__name__)


def load_rest_sites(sites_file):
    """
    load restriction sites(positions) into memory.
    """
    rest_sites = {}
    if sites_file.endswith(".gz"):
        with gzip.open(sites_file) as f:
            for idx, line in enumerate(f):
                line = line.decode('utf-8')
                chr_, start, end, _, _, _ = parse_line_bed6(line)
                if idx == 0:
                    rest_site_len = end - start
                rest_sites.setdefault(chr_, [])
                rest_sites[chr_].append(start)
        for k in rest_sites.keys():
            rest_sites[k] = np.asarray(rest_sites[k])
    else:  # hdf5 file
        with h5py.File(sites_file) as f:
            rest_site_len = len(f.attrs['rest_seq'])
            chromosomes = list(f['chromosomes'].keys())
            for chr_ in chromosomes:
                rest_sites[chr_] = f['chromosomes'][chr_].value
    return rest_sites, rest_site_len


def find_frag(rest_sites, start, end, rest_site_len):
    """
    Assign genome interval to fragment.

    Parameters
    ----------
    rest_sites : `numpy.ndarray`
        restriction sites(positions) of a chromosome.
    start : int
        PET start position
    end : int
        end position
    rest_site_len : int
        Length of restriction site.

    Return
    ------
    n : int
        Index number of fragment.
    pos : {'s', 'e'}
        fragment start or end.
    """
    end_search_point = end - rest_site_len - 1
    assert start < end_search_point
    sites = rest_sites
    mid = (start + end) // 2
    frag_idx_s = sites.searchsorted(start, side='right')
    frag_idx_e = sites.searchsorted(end_search_point,   side='right')
    if frag_idx_s == frag_idx_e:  # PET start and end located into same fragment
        frag_idx = frag_idx_s
        frag_start = sites[frag_idx - 1]
        frag_end = sites[frag_idx]

        if frag_idx == 0:
            pos = 'e'
        elif frag_idx == sites.shape[0]:
            pos = 's'
        else:
            left_span = mid - frag_start
            right_span = frag_end - mid
            if left_span < right_span:
                pos = 's'
            else:
                pos = 'e'
        return (frag_idx, pos)
    else:
        frag_start = sites[frag_idx_s - 1]
        mid_frag_pos = sites[frag_idx_s]
        frag_end = sites[frag_idx_e]

        if frag_idx_s == 0:
            frag_idx = frag_idx_s
            pos = 'e'
        elif frag_idx_e == sites.shape[0]:
            frag_idx = frag_idx_e
            pos = 's'
        else:
            left_span = start - frag_start
            right_span = frag_end - end_search_point - 1
            if left_span < right_span:
                pos = 's'
                frag_idx = frag_idx_s
            else:
                pos = 'e'
                frag_idx = frag_idx_e

#        print(frag_idx_s, frag_idx_e, "|", start, mid_frag_pos, end, "|", frag_start, frag_end, "|", left_span, right_span, "|", frag_idx, pos)
        
        return (frag_idx, pos)


def read_chunk(file, chunk_size=CHUNK_SIZE):
    """ read and parse bedpe file,
    return a list of bedpe items
    """
    chunk = []
    for i in range(chunk_size):
        line = file.readline()
        if not line:
            if i == 0:
                raise StopIteration
            else:
                return chunk
        items = parse_line_bedpe(line)
        chunk.append(items)
    return chunk


def bedpe_type(rest_sites, bedpe_items, threshold_span, rest_site_len):
    """
    judge interaction type,

    Parameters
    ----------
    rest_sites : dict
        Each chromosome's restriction start positions.
    bedpe_items : list
        bedpe fields.
    threshold_span : int
        If span large than this, will not check.
        Use -1 to force assign PET to fragment.
    rest_site_len : int
        Length of restriction site.

    Return
    ------
    types: {"normal", "self-ligation", "re-ligation"}
    frag1: {(int, {'s', 'e'}), None}
    frag2: {(int, {'s', 'e'}), None}
    """
    chr1, start1, end1, chr2, start2, end2 = bedpe_items[:6]
    # inter chromosome interaction
    if chr1 != chr2:
        if threshold_span != -1:
            return "normal", None, None
        else:
            frag1 = find_frag(rest_sites[chr1], start1, end1, rest_site_len)
            frag2 = find_frag(rest_sites[chr2], start2, end2, rest_site_len)
            return "normal", frag1, frag2

    # intra chromosome interaction
    span =  abs(start1 - start2)
    if span > threshold_span and threshold_span != -1:
        # safe span: interaction distance enough long
        if threshold_span != -1:
            return "normal", None, None
    else:
        # unsafe span, need to check
        sites = rest_sites[chr1]
        frag1 = find_frag(sites, start1, end1, rest_site_len)
        frag2 = find_frag(sites, start2, end2, rest_site_len)

        if frag1[0] == frag2[0]:
            return "self-ligation", frag1, frag2
        elif (frag2[0] - frag1[0] == 1) and frag2[1] == 's' and frag1[1] == 'e':
            return "re-ligation", frag1, frag2
        else:
            return "normal", frag1, frag2


def worker(task_queue,
           rest_sites, rest_site_len,
           output_file, err_sel_file, err_re_file,
           threshold_span, counts, lock):
    current = mp.current_process().pid
    output_f = open(output_file, 'w')
    err_sel_f = open(err_sel_file, 'w')
    err_re_f = open(err_re_file, 'w')
    l_counts = [0, 0, 0] # local counts, (normal, sel, re)
    while 1:
        try:
            chunk = task_queue.get()
            if chunk is None:
                raise Empty
        except Empty:
            output_f.close()
            err_sel_f.close()
            err_re_f.close()
            lock.acquire()
            counts['normal'] += l_counts[0]
            counts['self-ligation'] += l_counts[1]
            counts['re-ligation'] += l_counts[2]
            lock.release()
            log.debug("Process-%d done."%current)
            break

        for items in chunk:
            try:
                type_, frag1, frag2 = bedpe_type(rest_sites, items, threshold_span, rest_site_len)
            except KeyError as e:
                log.warning(str(e))
                continue

            out_line = "\t".join([str(i) for i in items])
            if frag1:
                out_line = out_line + "\t{}-{}\t{}-{}".format(frag1[0], frag1[1], frag2[0], frag2[1])
            out_line += "\n"

            if type_ == 'normal':
                output_f.write(out_line)
                l_counts[0] += 1
            elif type_ == 'self-ligation':
                err_sel_f.write(out_line)
                l_counts[1] += 1
            elif type_ == 're-ligation':
                err_re_f.write(out_line)
                l_counts[2] += 1


def log_counts(counts, log_file):
    log.info("Noise reduce result count:")
    total = sum(list(counts.values()))
    n = counts['normal']
    s = counts['self-ligation']
    r = counts['re-ligation']
    n_r = 0 if total == 0 else n / total
    s_r = 0 if total == 0 else s / total
    r_r = 0 if total == 0 else r / total
    msg1 = "normal\t{}\tpercent\t{:.2%}".format(n, n_r)
    msg2 = "self-ligation\t{}\tpercent\t{:.2%}".format(s, s_r)
    msg3 = "re-ligation\t{}\tpercent\t{:.2%}".format(r, r_r)
    msg4 = "total\t{}".format(total)
    log.info("\t" + msg1)
    log.info("\t" + msg2)
    log.info("\t" + msg3)
    log.info("\t" + msg4)
    with open(log_file, 'w') as f:
        f.write("normal\t{}\n".format(n))
        f.write("self-ligation\t{}\n".format(s))
        f.write("re-ligation\t{}\n".format(r))
        f.write("total\t{}\n".format(total))


@click.command(name="noise_reduce")
@click.argument("bedpe")
@click.argument("output")
@click.option("--restriction", "-r",
    required=True,
    help="bed file which recorded the position of all restriction sites"
        "in the genome.")
@click.option("--processes", "-p", 
    default=1,
    help="Use how many processes to run.")
@click.option("--threshold-span", "-s",
    default=1000,
    show_default=True,
    help="Threshold of pair span. Use -1 to force check.")
@click.option("--log-file",
    default="noise_reduce.log",
    help="Sperate log file for storage some count information.")
def _main(bedpe, output,
         restriction, processes,
         threshold_span,
         log_file):
    """
    Remove noise in DLO-HiC data.

    \b
    There are two kinds of removable abnormal intra chromosomal interacting pair:
        1. self-ligation.
        2. re-ligation

    \b
    In first situation, 
    PET1 and PET2 to will be assigned to same fragment.

    \b
    normal:
                           (frag n)         (frag m, m > n+1)
                           PET1             PET2
                           --------       --------
        genome    <---------------*---..--*--------------->
                               ^       ^
                               restriction sites

    \b
    abnormal-1 (self-ligation):
                            (frag n)   (frag n)
                            PET1       PET2
                            --------   --------
        genome    <---------*-------...-------*---------->
                            ^                 ^
                            restriction sites


    \b
    In second situation,
    PET1 and PET2 will be assigned to adjcent fragment.

    \b
    abnormal-2 (re-ligation):
                                      (frag n+1)
                                      PET2
                                      --------
                               --------
                               PET1 (frag n)
        genome    <-------------------*--------------------->


    """

    log.info("noise reduce on file %s"%bedpe)

    log.info("loading restriction sites from file: {}".format(restriction))
    rest_sites, rest_site_len = load_rest_sites(restriction)

    # init counts
    lock = mp.Lock()
    counts = mp.Manager().dict()
    counts['normal'] = 0
    counts['self-ligation'] = 0
    counts['re-ligation'] = 0

    task_queue = mp.Queue()
    workers = [mp.Process(target=worker,
                          args=(task_queue,
                                rest_sites, rest_site_len,
                                output+".tmp.%d"%i, output+".tmp.sel.%d"%i, output+".tmp.re.%d"%i,
                                threshold_span,
                                counts, lock))
               for i in range(processes)]

    log.info("%d workers spawned for noise refuce"%len(workers))

    for w in workers:
        w.start()

    with open(bedpe) as f:
        while 1:
            try:
                task_queue.put(read_chunk(f))
            except StopIteration:
                break

    for w in workers:
        task_queue.put(None)

    for w in workers:
        w.join()

    log.info("merging tmporary files.")
    # merge tmp files
    tmp_files = [output+".tmp.%d"%i for i in range(processes)]
    merge_tmp_files(tmp_files, output)
    tmp_sel_files = [output+".tmp.sel.%d"%i for i in range(processes)]
    merge_tmp_files(tmp_sel_files, output+".sel")
    tmp_re_files = [output+".tmp.re.%d"%i for i in range(processes)]
    merge_tmp_files(tmp_re_files, output+".re")

    log_counts(counts, log_file)
    log.info("Noise reduce done.")


main = _main.callback


if __name__ == "__main__":
    eval("_main()")
