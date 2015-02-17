import ast
import os
import re

from magic import Magic

from django.contrib.contenttypes.models import ContentType
from django.db.models import Q

from tardis.tardis_portal.models import (
    Dataset, DataFile, DataFileObject,
    ParameterName, DatafileParameterSet, DatafileParameter,
    ExperimentParameter,
    Schema, DatasetParameterSet, DatasetParameter,
    StorageBox, StorageBoxOption
)
from tardis.tardis_portal.util import generate_file_checksums

import logging
log = logging.getLogger(__name__)


def get_or_create_storage_box(datafile):
    key_name = 'datafile_id'
    class_name = 'tardis.tardis_portal.storage.squashfs.SquashFSStorage'
    try:
        s_box = StorageBoxOption.objects.get(
            key=key_name, value=datafile.id,
            storage_box__django_storage_class=class_name).storage_box
    except StorageBoxOption.DoesNotExist:
        s_box = StorageBox(
            django_storage_class=class_name,
            max_size=datafile.size,
            status='empty',
            name=datafile.filename,
            description='SquashFS Archive in DataFile id: %d, filename: %s' %
            (datafile.id, datafile.filename)
        )
        s_box.save()
        StorageBoxOption(key=key_name, value=datafile.id,
                         storage_box=s_box).save()
    return s_box


def get_squashfs_metadata(squash_sbox):
    '''
    squash file metadata

    path: frames/.info
    example contents:
        {'EPN': '8020l',
         u'PI': {u'Email': u'tom.caradoc-davies@synchrotron.org.au',
                 u'Name': u'Tom Caradoc-Davies',
                 u'ScientistID': u'783'},
         u'finishBooking': u'2014-07-12 08:00:00',
         u'handover': None,
         u'proposalType': u'MD',
         u'startBooking': u'2014-07-11 08:00:00',
         u'users': []}

        {'EPN': '8107b',
         u'PI': {u'Email': u'maria.hrmova@adelaide.edu.au',
                 u'Name': u'Maria Hrmova',
                 u'ScientistID': u'1886'},
         u'finishBooking': u'2014-08-01 16:00:00',
         u'handover': None,
         u'proposalType': u'CBR',
         u'startBooking': u'2014-08-01 08:00:00',
         u'users': [{u'Email': u'maria.hrmova@adelaide.edu.au',
                     u'Name': u'Maria Hrmova',
                     u'ScientistID': u'1886'},
                    {u'Email': u'victor.streltsov@csiro.au',
                     u'Name': u'Victor Streltsov',
                     u'ScientistID': u'183'}]}

    '''
    info_path = 'frames/.info'
    inst = squash_sbox.get_initialised_storage_instance()
    with inst.open(info_path) as info_file:
        info = ast.literal_eval(info_file.read())

    def transform_name(name):
        '''
        create short name from last name and first character of first name
        '''
        f_name, l_name = name.split(' ')
        u_name = l_name + f_name[0]
        u_name = u_name.lower()
        return u_name

    try:
        info['usernames'] = {
            transform_name(info['PI']['Name']): info['PI']}
        for user in info['users']:
            info['usernames'][transform_name(user['Name'])] = user
    except:
        pass

    return info


def remove_dotslash(path):
    if path[0:2] == './':
        return path[2:]
    return path


def auto_indexing_link(raw_datafile, indexing_dataset):
    auto_processing_schema = 'http://store.synchrotron.org.au/mx/indexing_link'
    schema, created = Schema.objects.get_or_create(
        name="AU Synchrotron MX auto indexing link",
        namespace=auto_processing_schema,
        type=Schema.DATAFILE,
        hidden=False)
    ps, created = DatafileParameterSet.objects.get_or_create(
        schema=schema, datafile=raw_datafile)
    pn, created = ParameterName.objects.get_or_create(
        schema=schema,
        name="auto indexing results",
        full_name="Link to dataset containing auto indexing results",
        data_type=ParameterName.LINK
    )
    par, created = DatafileParameter.objects.get_or_create(
        name=pn,
        parameterset=ps,
        link_id=indexing_dataset.id,
        link_ct=ContentType.objects.get_for_model(Dataset)
    )


def auto_processing_link(raw_dataset, auto_dataset):
    auto_processing_schema = 'http://store.synchrotron.org.au/mx/auto_link'
    schema, created = Schema.objects.get_or_create(
        name="AU Synchrotron MX auto processing link",
        namespace=auto_processing_schema,
        type=Schema.DATASET,
        hidden=False)
    ps, created = DatasetParameterSet.objects.get_or_create(
        schema=schema, dataset=raw_dataset)
    pn, created = ParameterName.objects.get_or_create(
        schema=schema,
        name="auto processing results",
        full_name="Link to dataset containing auto processing results",
        data_type=ParameterName.LINK
    )
    par, created = DatasetParameter.objects.get_or_create(
        name=pn,
        parameterset=ps,
        link_id=auto_dataset.id,
        link_ct=ContentType.objects.get_for_model(Dataset)
    )


def store_auto_id(dataset, auto_id):
    ns = 'http://synchrotron.org.au/mx/autoprocessing/xds'
    schema, created = Schema.objects.get_or_create(
        name="Synchrotron Auto Processing Results",
        namespace=ns,
        type=Schema.NONE,
        hidden=True)
    ps, created = DatasetParameterSet.objects.get_or_create(
        schema=schema, dataset=dataset)
    pn_mongoid, created = ParameterName.objects.get_or_create(
        schema=schema,
        name='mongo_id',
        full_name='Mongo DB ID',
        data_type=ParameterName.STRING
    )
    p_mongoid, created = DatasetParameter.objects.get_or_create(
        name=pn_mongoid, parameterset=ps)
    if p_mongoid.string_value is None or p_mongoid.string_value == '':
        p_mongoid.string_value = auto_id
        p_mongoid.save()


class ASSquashParser(object):
    '''
    if frames:
        files: .info
        directories:
            if calibration:
                all into calibration dataset
            else:
                if existing:
                    update existing dataset with directory
                else:
                    add file to 'missing'
    elif home:
        files: if not in ignore list: add to 'home'
        directories:
            if in ignore list: ignore
            else traverse:
                directories:
                    if handler defined: use handler
                    else add as dataset
                files:
                    if handler defined: use handler
                    else add to 'home' dataset
    else:
        add all to 'other'

    '''

    frames_ignore_paths = ['crystalpics', 'diffpics']

    typical_home = {
        'Desktop': {'description': 'Desktop folder'},
        'Documents': {'description': 'Documents folder'},
        'Downloads': {'description': 'Downloads folder'},
        'IDLWorkspace': {'description': 'IDL Workspace folder',
                         'ignore': True},
        'Music': {'description': 'Music folder',
                  'ignore': True},
        'Pictures': {'description': 'Pictures folder'},
        'Public': {'description': 'Public folder'},
        'Templates': {'description': 'Templates folder'},
        'Videos': {'description': 'Videos folder'},
        'areavision': {'description': 'Area Vision settings',
                       'ignore': True},
        'camera_settings': {'description': 'Camera settings',
                            'ignore': True},
        'chromium': {'description': 'Chromium folder',
                     'ignore': True},
        'edm_files': {'description': 'EDM files',
                      'ignore': True},
        'google-chrome': {'description': 'bad chrome symlink',
                          'ignore': True},
        'restart_logs': {'description': 'Restart logs',
                         'ignore': True},
        'sync': {'description': 'Sync folder',
                 'ignore': True},
        'xtal_info': {'description': 'Xtal info folder (Xtalview?)',
                      'ignore': True},
        '': {'description': 'other files'},
    }

    def __init__(self, squashfile, ns):
        self.epn = DatafileParameterSet.objects.get(
            datafile=squashfile,
            schema__namespace=ns
        ).datafileparameter_set.get(
            name__name='EPN'
        ).string_value

        exp_ns = 'http://www.tardis.edu.au/schemas/as/experiment/2010/09/21'
        parameter = ExperimentParameter.objects.get(
            name__name='EPN',
            name__schema__namespace=exp_ns,
            string_value=self.epn)
        self.experiment = parameter.parameterset.experiment
        self.s_box = get_or_create_storage_box(squashfile)
        self.metadata = get_squashfs_metadata(self.s_box)

        self.sq_inst = self.s_box.get_initialised_storage_instance()

    def parse(self):
        top = '.'
        dirnames, filenames = self.listdir('.')
        def_dataset = self.get_or_create_dataset('other files')
        result = self.add_files(top, filenames, def_dataset)
        for dirname in dirnames:
            if dirname == 'frames':
                result = result and self.parse_frames()
            elif dirname == 'home':
                result = result and self.parse_home()
        return result

    def parse_frames(self):
        '''
        add calibration frames to calibration dataset
        add all other files without changes
        '''
        top = 'frames'
        dirnames, filenames = self.listdir(top)
        result = self.add_files(top, filenames)
        if 'calibration' in dirnames:
            cal_dataset = self.get_or_create_dataset(
                'calibration', os.path.join(top, 'calibration'))
            result = result and self.add_subdir(top, cal_dataset)
            dirnames.remove('calibration')
        return result and all([self.add_subdir(os.path.join(top, dirname),
                                               ignore=self.frames_ignore_paths)
                               for dirname in dirnames])

    def parse_home(self):
        top = 'home'
        dirnames, filenames = self.listdir(top)
        home_dataset = self.get_or_create_dataset('home folder', top)
        result = self.add_files(top, filenames, home_dataset)
        for dirname in set(dirnames) & set(self.typical_home.keys()):
            if self.typical_home[dirname].get('ignore', False):
                continue
            dataset = self.get_or_create_dataset(
                self.typical_home[dirname]['description'],
                os.path.join(top, dirname))
            result = result and self.add_subdir(os.path.join(top, dirname),
                                                dataset)
        for dirname in set(dirnames) - set(self.typical_home.keys()):
            result = result and self.parse_user_dir(dirname)
        return result

    def parse_user_dir(self, userdir):
        top = os.path.join('home', userdir)
        dirnames, filenames = self.listdir(top)
        user_dataset = self.get_or_create_dataset(
            'home/%s' % userdir, top)
        result = self.add_files(top, filenames, user_dataset)
        if 'auto' in dirnames:
            result = result and self.parse_auto_processing(userdir)
            dirnames.remove('auto')
        return result and all([
            self.add_subdir(os.path.join(top, dirname), user_dataset)
            for dirname in dirnames])

    def parse_auto_processing(self, userdir):
        '''
        parse the auto folder under usernames.
        create indexing and xtal processing datasets and link them up to their
        raw data source datasets
        '''
        top = os.path.join('home', userdir, 'auto')
        dirnames, filenames = self.listdir(top)
        result = True
        if 'indexing_results.txt' in filenames:
            result = result and self.parse_indexing_results(userdir)
            result = self.add_files(top, [
                'indexing_results.txt',
                'indexing_results.html'
            ], self.get_or_create_dataset('indexing summary for %s' %
                                          userdir, top))
            filenames.remove('indexing_results.txt')
            filenames.remove('indexing_results.html')
            dirnames.remove('index')
        if 'dataset' in dirnames:
            result = result and self.parse_auto_dataset(userdir)
            dirnames.remove('dataset')

        if len(filenames) > 0:
            result = self.add_files(top, filenames, self.get_or_create_dataset(
                'other auto-files for %s' % userdir, top))

    def parse_indexing_results(self, userdir):
        '''
        adds a dataset for each indexing run and links it to the frame that
        was indexed
        '''
        top = os.path.join('home', userdir, 'auto')
        top_index = os.path.join(top, 'index')
        dirnames, filenames = self.listdir(top_index)
        with self.sq_inst.open(
                os.path.join(top, 'indexing_results.txt')) as infile:
            indexing_results = infile.readlines()[3:]
        result = True
        for line in indexing_results:
            items = line.split(',')
            raw_dir, raw_file = items[9:11]
            failed = items[15] == 'indexing failed'
            raw_path = os.path.join(*(raw_dir.split('/')[3:] + [raw_file]))
            raw_datafile = DataFile.objects.get(
                file_objects__uri__endswith=raw_path,
                dataset__experiments=self.experiment)
            # '([A-Za-z0-9_]+)_([0-9]+)_([0-9]+)(failed)?$'
            regex = re.compile(
                os.path.splitext(raw_file)[0] + '_([0-9]+)$')
            match = [m for m in dirnames if regex.match(m)][0]
            dirnames.remove(match)
            if failed:
                filenames.remove('%sfailed' % match)
            ds_dir = os.path.join(top_index, match)
            dataset = self.get_or_create_dataset(
                'Indexing for %(dataset)s, user %(userdir)s%(failed)s' % {
                    'dataset': raw_datafile.filename,
                    'userdir': userdir,
                    'failed': ' - failed' if failed else ''
                }, ds_dir)
            result = result and self.add_subdir(ds_dir, dataset=dataset)
            auto_indexing_link(raw_datafile, dataset)
        if len(dirnames) > 0 or len(filenames) > 0:
            other_ds = self.get_or_create_dataset(
                'other index-files for %s' % userdir, top)
        if len(filenames) > 0:
            result = result and self.add_files(top, filenames, other_ds)
        if len(dirnames) > 0:
            result = result and all([self.add_subdir(top, dirname, other_ds)
                                     for dirname in dirnames])
        return result

    def parse_auto_dataset(self, userdir):
        top = os.path.join('home', userdir, 'auto', 'dataset')
        dirnames, filenames = self.listdir(top)
        regex = re.compile(
            '(xds_process)?_?([a-z0-9_-]+)_([0-9]+)_([0-9a-fA-F]+)')
        # groups: xds y/n,   dataset name, image number, auto_id if xds
        other_dirs = []
        result = True
        for dirname in dirnames:
            match = regex.match(dirname)
            ds_dir = os.path.join(top, dirname)
            if match:
                ds_dir = os.path.join(top, dirname)
                dataset = self.get_or_create_dataset(
                    'Auto processing %s for %s' % (dirname, userdir),
                    ds_dir)
                result = result and self.add_subdir(ds_dir, dataset)
                logfile = '%s.log' % dirname
                if logfile in filenames:
                    result = result and self.add_file(top, logfile, dataset)
                    filenames.remove(logfile)
                raw_dataset = ' '

                dataset_path = os.readlink(
                    self.sq_inst.path(os.path.join(ds_dir, 'img')))
                dataset_path = '/'.join(dataset_path.split('/')[2:])
                img_dfos = DataFileObject.objects.filter(
                    datafile__dataset__experiments=self.experiment,
                    uri__contains=dataset_path)
                if len(img_dfos) > 0:
                    raw_dataset = img_dfos[0].datafile.dataset
                    if match.groups()[0] is not None:
                        store_auto_id(raw_dataset, match.groups()[3])
                    auto_processing_link(raw_dataset, dataset)
            else:
                other_dirs.append(dirname)
        if len(other_dirs) > 0:
            other_ds = self.get_or_create_dataset(
                'Auto processing other files for %s' % userdir, top)
            for dirname in other_dirs:
                result = result and self.add_subdir(dirname, other_ds)
        result = result and self.add_files(top, filenames, other_ds)
        return result

    def add_file(self, top, filename, dataset=None):
        if self.find_existing_dfo(top, filename):
            return True
        else:
            return self.create_dfo(top, filename, dataset)

    def add_files(self, top, filenames, dataset=None):
        if len(filenames) == 0:
            return True
        return all([self.add_file(top, filename, dataset)
                    for filename in filenames])

    def add_subdir(self, subdir, dataset=None, ignore=None):
        '''
        add a subdirectory and all children
        ignore folders that are defined in the ignore list
        '''
        dirnames, filenames = self.listdir(subdir)
        if ignore is not None:
            for path in ignore:
                if path in dirnames:
                    dirnames.remove(path)
        result = True
        result = result and self.add_files(subdir, filenames, dataset)
        if len(dirnames) > 0:
            result = result and all([self.add_subdir(dirname, dataset)
                                     for dirname in dirnames])
        return result

    def create_dfo(self, top, filename, dataset=None):
        '''
        create dfo and datafile if necessary
        '''
        df, df_data = self.find_datafile(top, filename)
        if df is None and df_data is None:
            return True  # is a link
        if df:
            if df.dataset != dataset:
                df.dataset = dataset
                df.save()
            self.update_dataset(df.dataset, top)
        else:
            if dataset is None:
                dataset = self.get_or_create_dataset('lost and found')
            df = DataFile(
                dataset=dataset,
                filename=filename,
                directory=top,
                **df_data)
            df.save()
        dfo = DataFileObject(
            datafile=df,
            storage_box=self.s_box,
            uri=os.path.join(top, filename)
        )
        dfo.save()
        return True

    def find_datafile(self, top, filename):
        fullpath = os.path.join(top, filename)
        # df_data usually is {md5, sha512, size, mimetype_buffer}
        df_data = self.get_file_details(
            top, filename)
        if df_data == {}:
            return None, None
        try:
            existing_dfs = DataFile.objects.filter(
                filename=filename,
                md5sum=df_data['md5sum'],
                size=df_data['size'],
                dataset__experiments=self.experiment)
            nodir = existing_dfs.filter(Q(directory=None) | Q(directory=''))
            samedir = existing_dfs.filter(directory=top)
            if nodir.count() == 1:
                existing_df = nodir[0]
                existing_df.directory = top
                existing_df.save()
            elif samedir.count() == 1:
                existing_df = samedir[0]
            else:
                existing_df = None
        except DataFile.DoesNotExist:
            existing_df = None
        df_data.update({
            'created_time': self.sq_inst.created_time(fullpath),
            'modification_time': self.sq_inst.modified_time(fullpath),
            # 'modified_time' is more standard, but will stick with df model
        })
        return existing_df, df_data

    def find_existing_dfo(self, top, filename):
        try:
            dfo = DataFileObject.objects.get(
                storage_box=self.s_box,
                uri=os.path.join(top, filename),
                datafile__dataset__experiments=self.experiment)
        except DataFileObject.DoesNotExist:
            dfo = False
        if dfo:
            self.update_dataset(dfo.datafile.dataset, top)
            return True
        return False

    def get_file_details(self, top, filename):
        fullpath = os.path.join(top, filename)
        try:
            md5, sha512, size, mimetype_buffer = generate_file_checksums(
                self.sq_inst.open(fullpath))
        except IOError as e:
            log.debug('squash parse error')
            log.debug(e)
            if os.path.islink(self.sq_inst.path(fullpath)):
                return {}
            raise
        mimetype = Magic(mime=True).from_buffer(mimetype_buffer or '')
        return {'size': str(size),
                'mimetype': mimetype,
                'md5sum': md5,
                'sha512sum': sha512}

    def get_or_create_dataset(self, name, top=None):
        '''
        returns existing or created dataset given a name

        returns False if the dataset is not unique by name

        top is the directory
        '''
        ds = Dataset.objects.filter(
            description=name, experiments=self.experiment)
        if len(ds) == 1:
            return ds[0]
        elif len(ds) > 1:
            return False
        ds = Dataset(description=name)
        if top is not None:
            ds.directory = top
            ds.save()
            self.tag_user(ds, top)
        else:
            ds.save()
        ds.experiments.add(self.experiment)
        return ds

    def listdir(self, top):
        try:
            dirnames, filenames = self.sq_inst.listdir(top)
        except os.error as err:
            log.debug(err)
            return [], []
        dirnames = [d for d in dirnames if not d.startswith('.')]
        filenames = [f for f in filenames if not f.startswith('.')]
        return dirnames, filenames

    def tag_user(self, dataset, path):
        elems = path.split(os.sep)
        username = None
        for elem in elems:
            if elem in self.metadata.get('usernames', []):
                username = elem
                break
        if username is None:
            return
        ns = 'http://synchrotron.org.au/userinfo'
        schema, created = Schema.objects.get_or_create(
            name="Synchrotron User Information",
            namespace=ns,
            type=Schema.NONE,
            hidden=True)
        ps, created = DatasetParameterSet.objects.get_or_create(
            schema=schema, dataset=dataset)
        pn_name, created = ParameterName.objects.get_or_create(
            schema=schema,
            name='name',
            full_name='Full Name',
            data_type=ParameterName.STRING
        )
        pn_email, created = ParameterName.objects.get_or_create(
            schema=schema,
            name='email',
            full_name='email address',
            data_type=ParameterName.STRING
        )
        pn_scientistid, created = ParameterName.objects.get_or_create(
            schema=schema,
            name='scientistid',
            full_name='ScientistID',
            data_type=ParameterName.STRING
        )
        data = self.metadata['usernames'][username]
        p_name, created = DatasetParameter.objects.get_or_create(
            name=pn_name, parameterset=ps)
        if p_name.string_value is None or p_name.string_value == '':
            p_name.string_value = data['Name']
            p_name.save()
        p_email, created = DatasetParameter.objects.get_or_create(
            name=pn_email, parameterset=ps)
        if p_email.string_value is None or p_email.string_value == '':
            p_email.string_value = data['Email']
            p_email.save()
        p_scientistid, created = DatasetParameter.objects.get_or_create(
            name=pn_scientistid, parameterset=ps)
        if p_scientistid.string_value is None or \
           p_scientistid.string_value == '':
            p_scientistid.string_value = data['ScientistID']
            p_scientistid.save()

    def update_dataset(self, dataset, top):
        if dataset.directory is None or not dataset.directory.startswith(top):
            dataset.directory = top
            dataset.save()
        self.tag_user(dataset, top)


def parse_squashfs_file(squashfile, ns):
    '''
    parse Australian Synchrotron specific SquashFS archive files
    '''

    parser = ASSquashParser(squashfile, ns)
    return parser.parse()
