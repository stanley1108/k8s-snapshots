"""Written in asyncio as a learning experiment. Python because the
backup expiration logic is already in tarsnapper and well tested.
"""

import os
import sys
import json
from datetime import datetime, timedelta
import asyncio
import confcollect
from aiochannel import Channel, ChannelEmpty
from googleapiclient import discovery
from oauth2client.service_account import ServiceAccountCredentials
from tarsnapper.config import parse_deltas, ConfigError
from tarsnapper.expire import expire
import pykube
import pendulum
import logbook
from asyncutils import combine, combine_latest, iterate_in_executor, exec


# TODO: the zone problem = formatting
# TODO: nicer names
# TODO: A solution to backup normal volumes?
#    -> manual list of volumes
# TODO: prevent a backup loop...
#

logger = logbook.Logger('daemon')


DEFAULT_CONFIG = {
    'log_level': 'INFO',
    'gcloud_project': '',
    'gcloud_json_keyfile_name': '',
    'gcloud_json_keyfile_string': '',
    'kube_config_file': ''
}


def validate_config(config):
    required_keys = ('gcloud_project',)

    result = True
    for key in required_keys:
        if not config.get(key):
            logbook.error('Environment variable {} is required', key.upper())
            result = False

    return result


class Context:

    def __init__(self, config=None):
        self.config = DEFAULT_CONFIG.copy()
        if config:
            self.config.update(config)
        self.kube = self.make_kubeclient()
        self.gcloud = self.make_gclient()

    def make_kubeclient(self):
        cfg = None

        if self.config.get('kube_config_file'):
            logger.info('Loading kube config from {}', self.config['kube_config_file'])
            cfg = pykube.KubeConfig.from_file(self.config['kube_config_file'])

        if not cfg:
            # See where we can get it from.
            default_file = os.path.expanduser('~/.kube/config')
            if os.path.exists(default_file):
                logger.info('Loading kube config from {}', default_file)
                cfg = pykube.KubeConfig.from_file(default_file)

        # Maybe we are running inside Kubernetes.
        if not cfg:
            logger.info('Using pod service account for kube auth')
            cfg = pykube.KubeConfig.from_service_account()

        return pykube.HTTPClient(cfg)

    def make_gclient(self):
        SCOPES = 'https://www.googleapis.com/auth/compute'
        credentials = None

        if self.config.get('gcloud_json_keyfile_name'):
            credentials = ServiceAccountCredentials.from_json_keyfile_name(
                self.config.get('gcloud_json_keyfile_name'),
                scopes=SCOPES)

        if self.config.get('gcloud_json_keyfile'):
            keyfile = json.loads(self.config.get('gcloud_json_keyfile_string'))
            credentials = ServiceAccountCredentials.from_json_keyfile_dict(
                keyfile, scopes=SCOPES)

        if not credentials:
            raise RuntimeError("Auth for Google Cloud was not configured")

        compute = discovery.build('compute', 'v1', credentials=credentials)
        return compute


class Rule:
    """A rule describes how and when to make backups.
    """

    name = None
    namespace = None
    deltas = None
    gce_disk = None
    gce_disk_zone = None

    @property
    def pretty_name(self):
        return self.name

    def __str__ (self):
        return self.name


def filter_snapshots_by_rule(snapshots, rule):
    def match_disk(snapshot):
        #europe-west1-c/disks/gke-production-c46cb51-pvc-01f74065-8fe9-11e6-abdd-42010af00148
        url_part = '/zones/{zone}/disks/{name}'.format(
            zone=rule.gce_disk_zone, name=rule.gce_disk)
        # TODO: Not obvious how we can know the zone..
        return snapshot['sourceDisk'].endswith(url_part)
    return filter(match_disk, snapshots)


def determine_next_snapshot(snapshots, rules):
    """Given a list of snapshots, and a list of rules, determine
    the next snapshot to be made.

    Returns a 2-tuple (rule, target_datetime)
    """
    next_rule = None
    next_timestamp = None

    for rule in rules:
        # Find all the snapshots that match this rule
        filtered = filter_snapshots_by_rule(snapshots, rule)
        # Rewrite the list to snapshot
        filtered = map(lambda s: pendulum.parse(s['creationTimestamp']), filtered)
        # Sort by timestamp
        filtered = sorted(filtered, reverse=True)
        filtered = list(filtered)

        # There are no snapshots for this rule; create the first one.
        if not filtered:
            next_rule = rule
            next_timestamp = pendulum.now('utc') + timedelta(seconds=10)
            break

        target = filtered[0] + rule.deltas[0]
        if not next_timestamp or target < next_timestamp:
            next_rule = rule
            next_timestamp =target

    return next_rule, next_timestamp


DELTA_ANNOTATION_KEY = 'backup.kubernetes.io/deltas'


def rule_from_pv(volume):
    """Given a persistent volume object, create a backup role
    object. Can return None if this volume is not configured for
    backups, or is not suitable.
    """

    # TODO: Currently, K8s does not allow a PersistetVolumeClaim to
    # specify any annotations for the PersistentVolume a provisioner
    # would create. Indeed, this might ever be possible. We might
    # want to follow the claimRef link and see if the claim specifies
    # any rules, and then use those.
    provider = volume.annotations.get('pv.kubernetes.io/provisioned-by')
    if provider != 'kubernetes.io/gce-pd':
        logger.debug('Volume {} not a GCE persistent disk', volume.name)
        return

    deltas = volume.annotations.get('backup.kubernetes.io/deltas')
    if not deltas:
        logger.debug('Volume {} does not define backup deltas (via {})',
            volume.name, DELTA_ANNOTATION_KEY)
        return

    try:
        deltas = parse_deltas(deltas)
    except ConfigError as e:
        logger.error('Deltas defined by volume {} are not valid, error message was: {}',
            volume.name, e)
        return


    # TODO: Use other names? name: volumeName / spec.claimRef.name claimRef.namespace
    rule = Rule()
    rule.name = volume.name
    rule.namespace = volume.namespace
    rule.deltas = deltas
    rule.gce_disk = volume.obj['spec']['gcePersistentDisk']['pdName']
    rule.gce_disk_zone = 'europe-west1-c'
    return rule


def sync_get_rules(ctx):
    rules = {}
    api = ctx.make_kubeclient()

    logger.debug('Observe persistent volume stream')
    stream = pykube.objects.PersistentVolume.objects(api).watch().object_stream()

    for event in stream:
        logger.debug('Event in persistent volume stream: {}', event)
        vid = event.object.name

        if event.type == 'ADDED' or event.type == 'MODIFIED':
            rule = rule_from_pv(event.object)
            if rule:
                if event.type == 'ADDED' or not vid in rules:
                    logger.info('Volume {} added to list of backup jobs', vid)
                else:
                    logger.info('Backup job for volume {} was updated', vid)
                rules[vid] = rule
            else:
                if vid in rules:
                    logger.info('Volume {} removed from list of backup jobs', vid)
                rules.pop(vid, False)

        if event.type == 'DELETED':
            rules.pop(vid, False)

        yield list(rules.values())


async def get_rules(ctx):
    async for item in iterate_in_executor(sync_get_rules, ctx):
        yield item


async def load_snapshots(ctx):
    r = await exec(ctx.gcloud.snapshots().list(project=ctx.config['gcloud_project']).execute)
    return r['items']


async def get_snapshots(ctx, reload_trigger):
    """Query the existing snapshots from Google Cloud.

    If the channel "reload_trigger" contains any value, we
    refresh the list of snapshots. This will then cause the
    next backup to be scheduled.
    """
    yield await load_snapshots(ctx)
    async for x in reload_trigger:
        yield await load_snapshots(ctx)


async def watch_schedule(ctx, trigger):
    """Continually yields the next backup to be created.

    It watches two input sources: the rules as defined by
    Kubernetes resources, and the existing snapshots, as returned
    from Google Cloud. If either of them change, a new backup
    is scheduled.
    """

    rulesgen = get_rules(ctx)
    snapgen = get_snapshots(ctx, trigger)

    async for item in combine_latest(
            rules=rulesgen, snapshots=snapgen, defaults={'snapshots': None, 'rules': None}):
        rules = item.get('rules')
        snapshots = item.get('snapshots')

        # Never schedule before we don't have data from both rules and snapshots
        if rules is None or snapshots is None:
            continue

        yield determine_next_snapshot(snapshots, rules)


async def make_backup(ctx, rule):
    """Execute a single backup job.

    1. Create the snapshot
    2. Wait until the snapshot is finished.
    3. Expire old snapshots
    """
    name = '{}-{}'.format(rule.pretty_name, pendulum.now('utc').format('%d%m%y-%H%M%S'))

    logbook.info('Creating a snapshot for disk {} with name {}',
        rule.pretty_name, name)

    result = await exec(ctx.gcloud.disks().createSnapshot(
        disk=rule.gce_disk,
        project=ctx.config['gcloud_project'],
        zone=rule.gce_disk_zone,
        body={"name": name}).execute)

    # Immediately after creating the snapshot, it sometimes seems to
    # take some seconds before it can be queried.
    await asyncio.sleep(10)

    logbook.debug('Waiting for snapshot to be ready')
    while result['status'] in ('PENDING', 'UPLOADING', 'CREATING'):
        await asyncio.sleep(2)
        result = await exec(ctx.gcloud.snapshots().get(
            snapshot=name,
            project=ctx.config['gcloud_project']).execute)

    if not result['status'] == 'READY':
        logger.error('Snapshot status is unexpected: {}', result['status'])
        return

    await expire_snapshots(ctx, rule)


async def expire_snapshots(ctx, rule):
    """Expire existing snapshots for the rule.
    """
    logbook.debug('Expire existing snapshots')

    snapshots = await load_snapshots(ctx)
    snapshots = filter_snapshots_by_rule(snapshots, rule)
    snapshots = {s['name']: pendulum.parse(s['creationTimestamp']) for s in snapshots}

    to_keep = expire(snapshots, rule.deltas)
    logbook.info('Out of {} snapshots, we want to keep {}',
        len(snapshots), len(to_keep))
    for snapshot_name in snapshots:
        if snapshot_name in to_keep:
            logbook.debug('Keeping snapshot {}', snapshot_name)
            continue

        if snapshot_name not in to_keep:
            logbook.info('Deleting snapshot {}', snapshot_name)
            result = await exec(ctx.gcloud.snapshots().delete(
                snapshot=snapshot_name,
                project=ctx.config['gcloud_project']).execute)


async def scheduler(ctx, scheduling_chan, snapshot_reload_trigger):
    """The "when to make a backup schedule" depends on the backup delta
    rules as defined in Kubernetes volume resources, and the existing
    snapshots.

    This simpy observes a stream of 'next planned backup' events and
    sends then to the channel given. Note that this scheduler
    doesn't plan multiple backups in advance. Only ever a single
    next backup is scheduled.
    """

    logger.info('Started scheduler task')

    async for schedule in watch_schedule(ctx, snapshot_reload_trigger):
        logger.debug('scheduler determined a new target backup')
        await scheduling_chan.put(schedule)


async def backuper(ctx, scheduling_chan, snapshot_reload_trigger):
    """Will take tasks from the given queue, then execute the backup.
    """
    logger.info('Started backup executor task')

    current_target_time = current_target_rule = None
    while True:
        await asyncio.sleep(1)

        try:
            current_target_rule, current_target_time = scheduling_chan.get_nowait()

            # Log a message
            if not current_target_time:
                backup_description = 'No backup scheduled'
            else:
                backup_description = '{0} at {1} ({2})'.format(
                    current_target_rule, current_target_time.in_timezone('utc'),
                    current_target_time.diff_for_humans())
            logger.info('Next scheduled backup changed: {}', backup_description)
        except ChannelEmpty:
            pass

        if not current_target_time:
            continue

        if pendulum.now('utc') > current_target_time:
            try:
                await make_backup(ctx, current_target_rule)
            finally:
                await snapshot_reload_trigger.put(True)
                current_target_time = current_target_rule = None


async def daemon(config):
    """Main app; it runs two tasks; one schedules backups, the other
    one executes the.
    """

    ctx = Context(config)

    # Using this channel, we can trigger a refresh of the list of
    # disk snapshots in the Google Cloud.
    snapshot_reload_trigger = Channel()

    # The backup task consumes this channel for the next backup task.
    scheduling_chan = Channel()

    schedule_task = asyncio.ensure_future(
        scheduler(ctx, scheduling_chan, snapshot_reload_trigger))
    backup_task = asyncio.ensure_future(
        backuper(ctx, scheduling_chan, snapshot_reload_trigger))
    await asyncio.gather(schedule_task, backup_task)


def main():
    config = DEFAULT_CONFIG.copy()
    config.update(confcollect.from_environ(by_defaults=DEFAULT_CONFIG))

    logbook.StderrHandler(level=config['log_level']).push_application()

    if not validate_config(config):
        return 1

    event_loop = asyncio.get_event_loop()
    try:
        event_loop.run_until_complete(daemon(config))
    finally:
        event_loop.close()


if __name__ == '__main__':
    sys.exit(main() or 0)