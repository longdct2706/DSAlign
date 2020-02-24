import os
import io
import sys
import csv
import math
import json
import wave
import random
import tarfile
import logging
import argparse
import statistics
import os.path as path

from datetime import timedelta
from collections import Counter
from multiprocessing import Pool
from audio import AUDIO_TYPE_PCM, AUDIO_TYPE_WAV, AUDIO_TYPE_OPUS,\
    ensure_wav_with_format, extract_audio, change_audio_types, write_audio_format_to_wav_file
from sample_collections import SortingSDBWriter, LabeledSample
from utils import parse_file_size, log_progress

UNKNOWN = '<UNKNOWN>'
AUDIO_TYPE_LOOKUP = {
    'wav': AUDIO_TYPE_WAV,
    'opus': AUDIO_TYPE_OPUS
}
SET_NAMES = ['train', 'dev', 'test']


def fail(message, code=1):
    logging.fatal(message)
    exit(code)


def engroup(lst, get_key):
    groups = {}
    for obj in lst:
        key = get_key(obj)
        if key in groups:
            groups[key].append(obj)
        else:
            groups[key] = [obj]
    return groups


def get_sample_size(population_size):
    margin_of_error = 0.01
    fraction_picking = 0.50
    z_score = 2.58  # Corresponds to confidence level 99%
    numerator = (z_score ** 2 * fraction_picking * (1 - fraction_picking)) / (
            margin_of_error ** 2
    )
    sample_size = 0
    for train_size in range(population_size, 0, -1):
        denominator = 1 + (z_score ** 2 * fraction_picking * (1 - fraction_picking)) / (
                margin_of_error ** 2 * train_size
        )
        sample_size = int(numerator / denominator)
        if 2 * sample_size + train_size <= population_size:
            break
    return sample_size


def load_segment(audio_path):
    result_path, _ = ensure_wav_with_format(audio_path, audio_format)
    return audio_path, result_path


def load_segment_dry(audio_path):
    if path.isfile(audio_path):
        logging.info('Would load file "{}"'.format(audio_path))
    else:
        fail('File not found: "{}"'.format(audio_path))
    return audio_path, audio_path


def parse_args():
    parser = argparse.ArgumentParser(description='Export aligned speech samples.')

    parser.add_argument('--audio', type=str,
                        help='Take audio file as input (requires "--aligned <file>")')
    parser.add_argument('--aligned', type=str,
                        help='Take alignment file ("<...>.aligned") as input (requires "--audio <file>")')

    parser.add_argument('--catalog', type=str,
                        help='Take alignment and audio file references of provided catalog ("<...>.catalog") as input')
    parser.add_argument('--ignore-missing', action="store_true",
                        help='Ignores catalog entries with missing files')

    parser.add_argument('--filter', type=str,
                        help='Python expression that computes a boolean value from sample data fields. '
                             'If the result is True, the sample will be dropped.')

    parser.add_argument('--criteria', type=str, default='100',
                        help='Python expression that computes a number as quality indicator from sample data fields.')

    parser.add_argument('--debias', type=str, action='append',
                        help='Sample meta field to group samples for debiasing (e.g. "speaker"). '
                             'Group sizes will be capped according to --debias-sigma-factor')
    parser.add_argument('--debias-sigma-factor', type=float, default=3.0,
                        help='Standard deviation (sigma) factor that will determine '
                             'the maximum number of samples per group (see --debias).')

    parser.add_argument('--partition', type=str, action='append',
                        help='Expression of the form "<number>:<partition>" where all samples with a quality indicator '
                             '(--criteria) above or equal the given number and below the next bigger one are assigned '
                             'to the specified partition. Samples below the lowest partition criteria are assigned to '
                             'partition "other".')

    parser.add_argument('--split', action="store_true",
                        help='Split each partition except "other" into train/dev/test sub-sets.')
    parser.add_argument('--split-field', type=str,
                        help='Sample meta field that should be used for splitting (e.g. "speaker")')
    parser.add_argument('--split-drop-multiple', action="store_true",
                        help='Drop all samples with multiple --split-field assignments.')
    parser.add_argument('--split-drop-unknown', action="store_true",
                        help='Drop all samples with no --split-field assignment.')
    for sub_set in SET_NAMES:
        parser.add_argument('--assign-' + sub_set,
                            help='Comma separated list of --split-field values that are to be assigned to sub-set '
                                 '"{}"'.format(sub_set))
    parser.add_argument('--split-seed', type=int,
                        help='Random seed for set splitting')

    parser.add_argument('--target-dir', type=str, required=False,
                        help='Existing target directory for storing generated sets (files and directories)')
    parser.add_argument('--target-tar', type=str, required=False,
                        help='Target tar-file for storing generated sets (files and directories)')
    parser.add_argument('--sdb', action="store_true",
                        help='Writes Sample DBs instead of CSV and .wav files (requires --target-dir)')
    parser.add_argument('--sdb-bucket-size', default='1GB',
                        help='Memory bucket size for external sorting of SDBs')
    parser.add_argument('--sdb-workers', type=int, default=None,
                        help='Number of SDB encoding workers')
    parser.add_argument('--sdb-buffered-samples', type=int, default=None,
                        help='Number of samples per bucket buffer during finalization')
    parser.add_argument('--sdb-audio-type', default='opus', choices=AUDIO_TYPE_LOOKUP.keys(),
                        help='Audio representation inside target SDBs')
    parser.add_argument('--buffer', default='1MB',
                        help='Buffer size for writing files (~16MB by default)')
    parser.add_argument('--force', action="store_true",
                        help='Overwrite existing files')
    parser.add_argument('--no-meta', action="store_true",
                        help='No writing of meta data files')
    parser.add_argument('--pretty', action="store_true",
                        help='Writes indented JSON output')
    parser.add_argument('--rate', type=int, default=16000,
                        help='Export wav-files with this sample rate')
    parser.add_argument('--channels', type=int, default=1,
                        help='Export wav-files with this number of channels')
    parser.add_argument('--width', type=int, default=2,
                        help='Export wav-files with this sample width (bytes)')

    parser.add_argument('--workers', type=int, default=2,
                        help='Number of workers for loading and re-sampling audio files. Default: 2')
    parser.add_argument('--dry-run', action="store_true",
                        help='Simulates export without writing or creating any file or directory')
    parser.add_argument('--dry-run-fast', action="store_true",
                        help='Simulates export without writing or creating any file or directory. '
                             'In contrast to --dry-run this faster simulation will not load samples.')
    parser.add_argument('--loglevel', type=int, default=20,
                        help='Log level (between 0 and 50) - default: 20')
    parser.add_argument('--no-progress', action="store_true",
                        help='Prevents showing progress indication')
    parser.add_argument('--progress-interval', type=float, default=1.0,
                        help='Progress indication interval in seconds')

    args = parser.parse_args()

    args.buffer = parse_file_size(args.buffer)
    args.sdb_bucket_size = parse_file_size(args.sdb_bucket_size)
    return args
    
    
def main():
    set_assignments = {}
    for set_index, set_name in enumerate(SET_NAMES):
        attr_name = 'assign_' + set_name
        if hasattr(CLI_ARGS, attr_name):
            set_entities = getattr(CLI_ARGS, attr_name)
            if set_entities is not None:
                for entity_id in str(set_entities).split(','):
                    if entity_id in set_assignments:
                        fail('Unable to assign entity "{}" to set "{}", as it is already assigned to set "{}"'
                             .format(entity_id, set_name, SET_NAMES[set_assignments[entity_id]]))
                    set_assignments[entity_id] = set_index

    logging.basicConfig(stream=sys.stderr, level=CLI_ARGS.loglevel if CLI_ARGS.loglevel else 20)
    logging.getLogger('sox').setLevel(logging.ERROR)

    def progress(it=None, desc='Processing', total=None):
        logging.info(desc)
        return it if CLI_ARGS.no_progress else log_progress(it, interval=CLI_ARGS.progress_interval, total=total)

    logging.debug("Start")

    pairs = []

    def check_path(target_path, fs_type='file'):
        if not (path.isfile(target_path) if fs_type == 'file' else path.isdir(target_path)):
            fail('{} not existing: "{}"'.format(fs_type[0].upper() + fs_type[1:], target_path))
        return path.abspath(target_path)

    def make_absolute(base_path, spec_path):
        if not path.isabs(spec_path):
            spec_path = path.join(base_path, spec_path)
        spec_path = path.abspath(spec_path)
        return spec_path if path.isfile(spec_path) else None

    target_dir = target_tar = None
    if CLI_ARGS.target_dir is not None and CLI_ARGS.target_tar is not None:
        fail('Only one allowed: --target-dir or --target-tar')
    elif CLI_ARGS.target_dir is not None:
        target_dir = check_path(CLI_ARGS.target_dir, fs_type='directory')
    elif CLI_ARGS.target_tar is not None:
        if CLI_ARGS.sdb:
            fail('Option --sdb not supported for --target-tar output. Use --target-dir instead.')
        target_tar = path.abspath(CLI_ARGS.target_tar)
        if path.isfile(target_tar):
            if not CLI_ARGS.force:
                fail('Target tar-file already existing - use --force to overwrite')
        elif path.exists(target_tar):
            fail('Target tar-file path is existing, but not a file')
        elif not path.isdir(path.dirname(target_tar)):
            fail('Unable to write tar-file: Path not existing')
    else:
        fail('Either --target-dir or --target-tar has to be provided')

    if CLI_ARGS.audio:
        if CLI_ARGS.aligned:
            pairs.append((check_path(CLI_ARGS.audio), check_path(CLI_ARGS.aligned)))
        else:
            fail('If you specify "--audio", you also have to specify "--aligned"')
    elif CLI_ARGS.aligned:
        fail('If you specify "--aligned", you also have to specify "--audio"')
    elif CLI_ARGS.catalog:
        catalog = check_path(CLI_ARGS.catalog)
        catalog_dir = path.dirname(catalog)
        with open(catalog, 'r') as catalog_file:
            catalog_entries = json.load(catalog_file)
        for entry in progress(catalog_entries, desc='Reading catalog'):
            audio = make_absolute(catalog_dir, entry['audio'])
            aligned = make_absolute(catalog_dir, entry['aligned'])
            if audio is None or aligned is None:
                if CLI_ARGS.ignore_missing:
                    continue
                if audio is None:
                    fail('Problem loading catalog "{}": Missing referenced audio file "{}"'
                         .format(CLI_ARGS.catalog, entry['audio']))
                if aligned is None:
                    fail('Problem loading catalog "{}": Missing referenced alignment file "{}"'
                         .format(CLI_ARGS.catalog, entry['aligned']))
            pairs.append((audio, aligned))
    else:
        fail('You have to either specify "--audio" and "--aligned" or "--catalog"')

    dry_run = CLI_ARGS.dry_run or CLI_ARGS.dry_run_fast
    load_samples = not CLI_ARGS.dry_run_fast

    partition_specs = []
    if CLI_ARGS.partition is not None:
        for partition_expr in CLI_ARGS.partition:
            parts = partition_expr.split(':')
            if len(parts) != 2:
                fail('Wrong partition specification: "{}"'.format(partition_expr))
            partition_specs.append((float(parts[0]), str(parts[1])))
    partition_specs.sort(key=lambda p: p[0], reverse=True)

    fragments = []
    for audio_path, aligned_path in progress(pairs, desc='Loading alignments'):
        with open(aligned_path, 'r') as aligned_file:
            aligned_fragments = json.load(aligned_file)
        for fragment in aligned_fragments:
            fragment['audio_path'] = audio_path
            fragments.append(fragment)

    if CLI_ARGS.filter is not None:
        kept_fragments = []
        for fragment in progress(fragments, desc='Filtering'):
            if not eval(CLI_ARGS.filter, {'math': math}, fragment):
                kept_fragments.append(fragment)
        if len(kept_fragments) < len(fragments):
            logging.info('Filtered out {} samples'.format(len(fragments) - len(kept_fragments)))
        fragments = kept_fragments
        if len(fragments) == 0:
            fail('Filter left no samples to export')

    for fragment in progress(fragments, desc='Computing qualities'):
        fragment['quality'] = eval(CLI_ARGS.criteria, {'math': math}, fragment)

    def get_meta_list(f, meta_field):
        if 'meta' in f:
            meta_fields = f['meta']
            if isinstance(meta_fields, dict) and meta_field in meta_fields:
                meta_field = meta_fields[meta_field]
                return meta_field if isinstance(meta_field, list) else [meta_field]
        return []

    def get_first_meta(f, meta_field):
        meta_field = get_meta_list(f, meta_field)
        return meta_field[0] if meta_field else UNKNOWN

    if CLI_ARGS.debias is not None:
        for debias in CLI_ARGS.debias:
            grouped = engroup(fragments, lambda f: get_first_meta(f, debias))
            if UNKNOWN in grouped:
                fragments = grouped[UNKNOWN]
                del grouped[UNKNOWN]
            else:
                fragments = []
            counts = list(map(lambda f: len(f), grouped.values()))
            mean = statistics.mean(counts)
            sigma = statistics.pstdev(counts, mu=mean)
            cap = int(mean + CLI_ARGS.debias_sigma_factor * sigma)
            counter = Counter()
            for group, values in progress(grouped.items(), desc='Debiasing "{}"'.format(debias)):
                if len(values) > cap:
                    values.sort(key=lambda g: g['quality'])
                    counter[group] += len(values) - cap
                    values = values[-cap:]
                fragments.extend(values)
            logging.info('Dropped for debiasing "{}":'.format(debias))
            for group, count in counter.most_common():
                logging.info(' - "{}": {}'.format(group, count))

    def get_partition(f):
        quality = f['quality']
        for minimum, partition_name in partition_specs:
            if quality >= minimum:
                return partition_name
        return 'other'

    lists = {}

    def assign_fragments(frags, name):
        if name not in lists:
            lists[name] = []
            if CLI_ARGS.target_dir is not None and not CLI_ARGS.force:
                paths = [name + '.sdb', name + '.sdb.tmp'] if CLI_ARGS.sdb else [name, name + '.csv']
                for p in paths:
                    if path.exists(path.join(target_dir, p)):
                        fail('"{}" already existing - use --force to ignore'.format(p))
        duration = 0
        for f in frags:
            f['list_name'] = name
            duration += (f['end'] - f['start'])
        logging.info('Built set "{}" (samples: {}, duration: {})'.format(name,
                                                                         len(frags),
                                                                         timedelta(milliseconds=duration)))

    if CLI_ARGS.split_seed is not None:
        random.seed(CLI_ARGS.split_seed)

    if CLI_ARGS.split and CLI_ARGS.split_field:
        if CLI_ARGS.split_drop_multiple:
            fragments = filter(lambda f: len(get_meta_list(f, CLI_ARGS.split_field)) < 2, fragments)
        if CLI_ARGS.split_drop_unknown:
            fragments = filter(lambda f: len(get_meta_list(f, CLI_ARGS.split_field)) > 0, fragments)
        fragments = list(fragments)
        metas = engroup(fragments, lambda f: get_first_meta(f, CLI_ARGS.split_field)).items()
        metas = sorted(metas, key=lambda meta_frags: len(meta_frags[1]))
        metas = list(map(lambda meta_frags: meta_frags[0], metas))
        partitions = engroup(fragments, get_partition)
        partitions = list(map(lambda part_frags: (part_frags[0],
                                                  get_sample_size(len(part_frags[1])),
                                                  engroup(part_frags[1], lambda pf: get_first_meta(pf, CLI_ARGS.split_field)),
                                                  [[], [], []]),
                              partitions.items()))
        remaining_metas = []
        for meta in metas:
            if meta in set_assignments:
                set_index = set_assignments[meta]
                for _, _, partition_portions, sample_sets in partitions:
                    if meta in partition_portions:
                        sample_sets[set_index].extend(partition_portions[meta])
                        del partition_portions[meta]
            else:
                remaining_metas.append(meta)
        metas = remaining_metas
        for _, sample_size, _, sample_sets in partitions:
            while len(metas) > 0 and (len(sample_sets[1]) < sample_size or len(sample_sets[2]) < sample_size):
                for sample_set_index, sample_set in enumerate(sample_sets):
                    if len(metas) > 0 and sample_size > len(sample_set):
                        meta = metas.pop(0)
                        for _, _, partition_portions, other_sample_sets in partitions:
                            if meta in partition_portions:
                                other_sample_sets[sample_set_index].extend(partition_portions[meta])
                                del partition_portions[meta]
        for partition, sample_size, partition_portions, sample_sets in partitions:
            for portion in partition_portions.values():
                sample_sets[0].extend(portion)
            for set_index, set_name in enumerate(SET_NAMES):
                assign_fragments(sample_sets[set_index], partition + '-' + set_name)
    else:
        partitions = engroup(fragments, get_partition)
        for partition, partition_fragments in partitions.items():
            if CLI_ARGS.split:
                sample_size = get_sample_size(len(partition_fragments))
                random.shuffle(partition_fragments)
                test_set = partition_fragments[:sample_size]
                partition_fragments = partition_fragments[sample_size:]
                dev_set = partition_fragments[:sample_size]
                train_set = partition_fragments[sample_size:]
                sample_sets = [train_set, dev_set, test_set]
                for set_index, set_name in enumerate(SET_NAMES):
                    assign_fragments(sample_sets[set_index], partition + '-' + set_name)
            else:
                assign_fragments(partition_fragments, partition)

    def list_fragments():
        audio_files = engroup(fragments, lambda f: f['audio_path'])
        pool = Pool(CLI_ARGS.workers)
        ls = load_segment if load_samples else load_segment_dry
        for original_path, converted_path in pool.imap_unordered(ls, audio_files.keys()):
            file_fragments = audio_files[original_path]
            file_fragments.sort(key=lambda f: f['start'])
            if load_samples:
                with wave.open(converted_path, 'rb') as source_wav_file:
                    duration = source_wav_file.getframerate() * source_wav_file.getnframes() * 1000
                    for fragment in file_fragments:
                        try:
                            start, end = fragment['start'], fragment['end']
                            assert start < end <= duration
                            fragment_audio = extract_audio(source_wav_file, start / 1000.0, end / 1000.0)
                        except Exception as ae:
                            raise RuntimeError('Problem getting audio for fragment\n{}"'
                                               .format(json.dumps(fragment, indent=4))) from ae
                        yield fragment_audio, fragment
                if original_path != converted_path:
                    os.remove(converted_path)
            else:
                for fragment in file_fragments:
                    yield b'', fragment

    if CLI_ARGS.sdb:
        audio_type = AUDIO_TYPE_LOOKUP[CLI_ARGS.sdb_audio_type]
        for list_name in lists.keys():
            sdb_path = os.path.join(target_dir, list_name + '.sdb')
            if dry_run:
                logging.info('Would create SDB "{}"'.format(sdb_path))
            else:
                logging.info('Creating SDB "{}"'.format(sdb_path))
                lists[list_name] = SortingSDBWriter(sdb_path,
                                                    audio_type=audio_type,
                                                    buffering=CLI_ARGS.buffer,
                                                    cache_size=CLI_ARGS.sdb_bucket_size,
                                                    buffered_samples=CLI_ARGS.sdb_buffered_samples)

        def to_samples():
            for pcm_data, f in list_fragments():
                cs = LabeledSample(AUDIO_TYPE_PCM, pcm_data, f['aligned'], audio_format=audio_format)
                cs.meta = f
                yield cs

        samples = change_audio_types(to_samples(),
                                     audio_type=audio_type,
                                     processes=CLI_ARGS.sdb_workers) if load_samples else to_samples()
        set_counter = Counter()
        for sample in progress(samples, desc='Exporting samples', total=len(fragments)):
            list_name = sample.meta['list_name']
            if not dry_run:
                set_counter[list_name] += 1
                sdb = lists[list_name]
                sdb.add(sample)
        for list_name, sdb in lists.items():
            meta_path = os.path.join(target_dir, list_name + '.meta')
            if not dry_run:
                for _ in progress(sdb.finalize(), desc='Finalizing {}'.format(list_name), total=set_counter[list_name]):
                    pass
                if not CLI_ARGS.no_meta:
                    logging.info('Writing meta file "{}"'.format(meta_path))
                    with open(meta_path, 'w') as meta_file:
                        json.dump(sdb.meta_dict, meta_file, indent=4 if CLI_ARGS.pretty else None)
            else:
                if not CLI_ARGS.no_meta:
                    logging.info('Would write meta file "{}"'.format(meta_path))
        return

    created_directories = {}
    tar = None
    if target_tar is not None:
        if dry_run:
            logging.info('Would create tar-file "{}"'.format(target_tar))
        else:
            base_tar = open(target_tar, 'wb', buffering=CLI_ARGS.buffer)
            tar = tarfile.open(fileobj=base_tar, mode='w')

    class TargetFile:
        def __init__(self, data_path, mode):
            self.data_path = data_path
            self.mode = mode
            self.open_file = None

        def __enter__(self):
            parts = self.data_path.split('/')
            dirs = ([target_dir] if target_dir is not None else []) + parts[:-1]
            for i in range(1, len(dirs)):
                vp = '/'.join(dirs[:i + 1])
                if not vp in created_directories:
                    if tar is None:
                        dir_path = path.join(*dirs[:i + 1])
                        if not path.isdir(dir_path):
                            if dry_run:
                                logging.info('Would create directory "{}"'.format(dir_path))
                            else:
                                os.mkdir(dir_path)
                    else:
                        tdir = tarfile.TarInfo(vp)
                        tdir.type = tarfile.DIRTYPE
                        tar.addfile(tdir)
                    created_directories[vp] = True
            if target_tar is None:
                file_path = path.join(target_dir, *self.data_path.split('/'))
                if dry_run:
                    logging.info('Would write file "{}"'.format(file_path))
                    self.open_file = io.BytesIO() if 'b' in self.mode else io.StringIO()
                else:
                    self.open_file = open(file_path, self.mode)
            else:
                self.open_file = io.BytesIO() if 'b' in self.mode else io.StringIO()
            return self.open_file

        def __exit__(self, *args):
            if tar is not None:
                if isinstance(self.open_file, io.StringIO):
                    sfile = self.open_file
                    sfile.seek(0)
                    self.open_file = io.BytesIO(sfile.read().encode('utf8'))
                    self.open_file.seek(0, 2)
                    sfile.close()
                tfile = tarfile.TarInfo(self.data_path)
                tfile.size = self.open_file.tell()
                self.open_file.seek(0)
                tar.addfile(tfile, self.open_file)
                tar.members = []
            if self.open_file is not None:
                self.open_file.close()

    for audio_segment, fragment in progress(list_fragments(), desc='Exporting samples', total=len(fragments)):
        list_name = fragment['list_name']
        group_list = lists[list_name]
        sample_path = '{}/sample-{:010d}.wav'.format(fragment['list_name'], len(group_list))
        with TargetFile(sample_path, "wb") as base_wav_file:
            with wave.open(base_wav_file, 'wb') as wav_file:
                write_audio_format_to_wav_file(wav_file)
                wav_file.writeframes(audio_segment)
                file_size = base_wav_file.tell()
        group_list.append((sample_path, file_size, fragment))

    for list_name, group_list in progress(lists.items(), desc='Writing lists'):
        if not CLI_ARGS.no_meta:
            entries = {}
            for rel_path, _, fragment in group_list:
                entries[rel_path] = fragment
            with TargetFile(list_name + '.meta', 'w') as meta_file:
                json.dump(entries, meta_file, indent=4 if CLI_ARGS.pretty else None)
        with TargetFile(list_name + '.csv', 'w') as csv_file:
            writer = csv.writer(csv_file)
            writer.writerow(['wav_filename', 'wav_filesize', 'transcript'])
            for rel_path, file_size, fragment in group_list:
                writer.writerow([rel_path, file_size, fragment['aligned']])

    if tar is not None:
        tar.close()


if __name__ == '__main__':
    CLI_ARGS = parse_args()
    audio_format = (CLI_ARGS.rate, CLI_ARGS.channels, CLI_ARGS.width)
    main()
