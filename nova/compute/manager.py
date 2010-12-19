# vim: tabstop=4 shiftwidth=4 softtabstop=4

# Copyright 2010 United States Government as represented by the
# Administrator of the National Aeronautics and Space Administration.
# All Rights Reserved.
#
#    Licensed under the Apache License, Version 2.0 (the "License"); you may
#    not use this file except in compliance with the License. You may obtain
#    a copy of the License at
#
#         http://www.apache.org/licenses/LICENSE-2.0
#
#    Unless required by applicable law or agreed to in writing, software
#    distributed under the License is distributed on an "AS IS" BASIS, WITHOUT
#    WARRANTIES OR CONDITIONS OF ANY KIND, either express or implied. See the
#    License for the specific language governing permissions and limitations
#    under the License.

"""
Handles all processes relating to instances (guest vms).

The :py:class:`ComputeManager` class is a :py:class:`nova.manager.Manager` that
handles RPC calls relating to creating instances.  It is responsible for
building a disk image, launching it via the underlying virtualization driver,
responding to calls to check its state, attaching persistent storage, and
terminating it.

**Related Flags**

:instances_path:  Where instances are kept on disk
:compute_driver:  Name of class that is used to handle virtualization, loaded
                  by :func:`nova.utils.import_object`
:volume_manager:  Name of class that handles persistent storage, loaded by
                  :func:`nova.utils.import_object`
"""

import datetime
import logging
import sys
import traceback
import os
import time


from twisted.internet import defer

from nova import exception
from nova import flags
from nova import manager
from nova import utils
from nova.compute import power_state
from nova import rpc
from nova import db

FLAGS = flags.FLAGS
flags.DEFINE_string('instances_path', '$state_path/instances',
                    'where instances are stored on disk')
flags.DEFINE_string('compute_driver', 'nova.virt.connection.get_connection',
                    'Driver to use for controlling virtualization')
flags.DEFINE_string('live_migration_timeout', 30,
                    'Timeout value for pre_live_migration is completed.')


class ComputeManager(manager.Manager):

    """Manages the running instances from creation to destruction."""

    def __init__(self, compute_driver=None, *args, **kwargs):
        """Load configuration options and connect to the hypervisor."""
        # TODO(vish): sync driver creation logic with the rest of the system
        #             and redocument the module docstring
        if not compute_driver:
            compute_driver = FLAGS.compute_driver
        self.driver = utils.import_object(compute_driver)
        self.network_manager = utils.import_object(FLAGS.network_manager)
        self.volume_manager = utils.import_object(FLAGS.volume_manager)
        super(ComputeManager, self).__init__(*args, **kwargs)

    def _update_state(self, context, instance_id):
        """Update the state of an instance from the driver info."""
        # FIXME(ja): include other fields from state?
        instance_ref = self.db.instance_get(context, instance_id)
        try:
            info = self.driver.get_info(instance_ref['name'])
            state = info['state']
        except exception.NotFound:
            state = power_state.NOSTATE
        self.db.instance_set_state(context, instance_id, state)

    @defer.inlineCallbacks
    @exception.wrap_exception
    def refresh_security_group(self, context, security_group_id, **_kwargs):
        """This call passes stright through to the virtualization driver."""
        yield self.driver.refresh_security_group(security_group_id)

    @defer.inlineCallbacks
    @exception.wrap_exception
    def run_instance(self, context, instance_id, **_kwargs):
        """Launch a new instance with specified options."""
        context = context.elevated()
        instance_ref = self.db.instance_get(context, instance_id)
        if instance_ref['name'] in self.driver.list_instances():
            raise exception.Error("Instance has already been created")
        logging.debug("instance %s: starting...", instance_id)
        self.network_manager.setup_compute_network(context, instance_id)
        self.db.instance_update(context,
                                instance_id,
                                {'host': self.host})

        # TODO(vish) check to make sure the availability zone matches
        self.db.instance_set_state(context,
                                   instance_id,
                                   power_state.NOSTATE,
                                   'spawning')

        try:
            yield self.driver.spawn(instance_ref)
            now = datetime.datetime.utcnow()
            self.db.instance_update(context,
                                    instance_id,
                                    {'launched_at': now})
        except Exception:  # pylint: disable-msg=W0702
            logging.exception("instance %s: Failed to spawn",
                              instance_ref['name'])
            self.db.instance_set_state(context,
                                       instance_id,
                                       power_state.SHUTDOWN)

        self._update_state(context, instance_id)

    @defer.inlineCallbacks
    @exception.wrap_exception
    def terminate_instance(self, context, instance_id):
        """Terminate an instance on this machine."""
        context = context.elevated()
        logging.debug("instance %s: terminating", instance_id)

        instance_ref = self.db.instance_get(context, instance_id)
        volumes = instance_ref.get('volumes', []) or []
        for volume in volumes:
            self.detach_volume(context, instance_id, volume['id'])
        if instance_ref['state'] == power_state.SHUTOFF:
            self.db.instance_destroy(context, instance_id)
            raise exception.Error('trying to destroy already destroyed'
                                  ' instance: %s' % instance_id)
        yield self.driver.destroy(instance_ref)

        # TODO(ja): should we keep it in a terminated state for a bit?
        self.db.instance_destroy(context, instance_id)

    @defer.inlineCallbacks
    @exception.wrap_exception
    def reboot_instance(self, context, instance_id):
        """Reboot an instance on this server."""
        context = context.elevated()
        instance_ref = self.db.instance_get(context, instance_id)
        self._update_state(context, instance_id)

        if instance_ref['state'] != power_state.RUNNING:
            logging.warn('trying to reboot a non-running '
                         'instance: %s (state: %s excepted: %s)',
                         instance_ref['internal_id'],
                         instance_ref['state'],
                         power_state.RUNNING)

        logging.debug('instance %s: rebooting', instance_ref['name'])
        self.db.instance_set_state(context,
                                   instance_id,
                                   power_state.NOSTATE,
                                   'rebooting')
        yield self.driver.reboot(instance_ref)
        self._update_state(context, instance_id)

    @defer.inlineCallbacks
    @exception.wrap_exception
    def rescue_instance(self, context, instance_id):
        """Rescue an instance on this server."""
        context = context.elevated()
        instance_ref = self.db.instance_get(context, instance_id)

        logging.debug('instance %s: rescuing',
                      instance_ref['internal_id'])
        self.db.instance_set_state(context,
                                   instance_id,
                                   power_state.NOSTATE,
                                   'rescuing')
        yield self.driver.rescue(instance_ref)
        self._update_state(context, instance_id)

    @defer.inlineCallbacks
    @exception.wrap_exception
    def unrescue_instance(self, context, instance_id):
        """Rescue an instance on this server."""
        context = context.elevated()
        instance_ref = self.db.instance_get(context, instance_id)

        logging.debug('instance %s: unrescuing',
                      instance_ref['internal_id'])
        self.db.instance_set_state(context,
                                   instance_id,
                                   power_state.NOSTATE,
                                   'unrescuing')
        yield self.driver.unrescue(instance_ref)
        self._update_state(context, instance_id)

    @exception.wrap_exception
    def get_console_output(self, context, instance_id):
        """Send the console output for an instance."""
        context = context.elevated()
        logging.debug("instance %s: getting console output", instance_id)
        instance_ref = self.db.instance_get(context, instance_id)

        return self.driver.get_console_output(instance_ref)

    @defer.inlineCallbacks
    @exception.wrap_exception
    def attach_volume(self, context, instance_id, volume_id, mountpoint):
        """Attach a volume to an instance."""
        context = context.elevated()
        logging.debug("instance %s: attaching volume %s to %s", instance_id,
            volume_id, mountpoint)
        instance_ref = self.db.instance_get(context, instance_id)
        dev_path = yield self.volume_manager.setup_compute_volume(context,
                                                                  volume_id)
        try:
            yield self.driver.attach_volume(instance_ref['name'],
                                            dev_path,
                                            mountpoint)
            self.db.volume_attached(context,
                                    volume_id,
                                    instance_id,
                                    mountpoint)
        except Exception as exc:  # pylint: disable-msg=W0702
            # NOTE(vish): The inline callback eats the exception info so we
            #             log the traceback here and reraise the same
            #             ecxception below.
            logging.exception("instance %s: attach failed %s, removing",
                              instance_id, mountpoint)
            yield self.volume_manager.remove_compute_volume(context,
                                                            volume_id)
            raise exc
        defer.returnValue(True)

    @defer.inlineCallbacks
    @exception.wrap_exception
    def detach_volume(self, context, instance_id, volume_id):
        """Detach a volume from an instance."""
        context = context.elevated()
        logging.debug("instance %s: detaching volume %s",
                      instance_id,
                      volume_id)
        instance_ref = self.db.instance_get(context, instance_id)
        volume_ref = self.db.volume_get(context, volume_id)
        if instance_ref['name'] not in self.driver.list_instances():
            logging.warn("Detaching volume from unknown instance %s",
                         instance_ref['name'])
        else:
            yield self.driver.detach_volume(instance_ref['name'],
                                            volume_ref['mountpoint'])
        yield self.volume_manager.remove_compute_volume(context, volume_id)
        self.db.volume_detached(context, volume_id)
        defer.returnValue(True)

    def get_vcpu_number(self):
        """Get the number of vcpu on physical computer."""
        return self.driver.get_vcpu_number()

    def get_mem_size(self):
        """Get the memory size of physical computer ."""
        meminfo = open('/proc/meminfo').read().split()
        idx = meminfo.index('MemTotal:')
        # transforming kb to mb.
        return int(meminfo[idx + 1]) / 1024

    def get_hdd_size(self):
        """Get the hdd size of physical computer ."""
        hddinfo = os.statvfs(FLAGS.instances_path)
        return hddinfo.f_bsize * hddinfo.f_blocks / 1024 / 1024 / 1024

    def pre_live_migration(self, context, instance_id, dest):
        """Any preparation for live migration at dst host."""

        # 1. getting volume info ( shlf/slot number )
        instance_ref = db.instance_get(context, instance_id)
        ec2_id = instance_ref['hostname']

        volumes = []
        try:
            volumes = db.volume_get_by_ec2_id(context, ec2_id)
        except exception.NotFound:
            logging.debug('%s has no volume.', ec2_id)

        shelf_slots = {}
        for vol in volumes:
            shelf, slot = db.volume_get_shelf_and_blade(context, vol['id'])
            shelf_slots[vol.id] = (shelf, slot)

        # 2. getting fixed ips
        fixed_ip = db.instance_get_fixed_address(context, instance_id)
        if None == fixed_ip:
            exc_type = 'NotFoundError'
            val = '%s(%s) doesnt have fixed_ip ' % (instance_id, ec2_id)
            tb = ''.join(traceback.format_tb(sys.exc_info()[2]))
            raise rpc.RemoteError(exc_type, val, tb)

        # 3. if any volume is mounted, prepare here.
        if 0 != len(shelf_slots):
            pass

        # 4. Creating nova-instance-instance-xxx, this is written to libvirt.xml, 
        #    and can be seen when executin "virsh nwfiter-list" On destination host,
        #    this nwfilter is necessary.
        #    In addition this method is creating security rule ingress rule onto
        #    destination host.
        self.driver.setup_nwfilters_for_instance(instance_ref)

        # 5. bridge settings
        self.network_manager.setup_compute_network(instance_id)
        return True

    def nwfilter_for_instance_exists(self, context, instance_id):
        """Check nova-instance-instance-xxx filter exists """
        instance_ref = db.instance_get(context, instance_id)
        return self.driver.nwfilter_for_instance_exists(instance_ref)

    def live_migration(self, context, instance_id, dest):
        """executes live migration."""

        # 1. ask dest host to preparing live migration.
        compute_topic = db.queue_get_for(context, FLAGS.compute_topic, dest)
        ret = rpc.call(context,
                        compute_topic,
                        {"method": "pre_live_migration",
                         "args": {'instance_id': instance_id,
                                    'dest': dest}})

        if True != ret:
            logging.error('Live migration failed(err at %s)', dest)
            db.instance_set_state(context,
                                  instance_id,
                                  power_state.RUNNING,
                                  'running')
            return

        # waiting for setting up nwfilter(nova-instance-instance-xxx)
        # otherwise, live migration fail.
        timeout_count = range(FLAGS.live_migration_timeout)
        while 0 != len(timeout_count):
            ret = rpc.call(context,
                        compute_topic,
                        {"method": "nwfilter_for_instance_exists",
                         "args": {'instance_id': instance_id}})
            if ret:
                break

            timeout_count.pop()
            time.sleep(1)

        if not ret:
            logging.error('Timeout for pre_live_migration at %s', dest)
            return

        # 2. executing live migration
        # live_migration might raises ProcessExecution error, but
        # nothing must be recovered in this version.
        instance_ref = db.instance_get(context, instance_id)
        ret = self.driver.live_migration(instance_ref, dest)
        if not ret:
            logging.debug('Fail to live migration')
            return
