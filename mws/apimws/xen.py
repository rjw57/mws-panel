from __future__ import absolute_import
import copy
import logging
import re
import tempfile
import uuid
import json
import subprocess
from celery import shared_task, Task
from django.conf import settings
from django.core.urlresolvers import reverse
from apimws.ipreg import set_sshfp
from apimws.models import Cluster
from apimws.views import post_installation, post_recreate
from mws.celery import app
from sitesmanagement.models import VirtualMachine, NetworkConfig, Service, SiteKey, Vhost, DomainName


LOGGER = logging.getLogger('mws')


class VMAPINotWorkingException(Exception):
    pass


class VMAPIInputException(Exception):
    pass


class VMAPIFailure(Exception):
    pass


def vm_api_request(**json_object):
    if not getattr(settings, 'VM_END_POINT_COMMAND', False):
        raise VMAPIFailure("VM_END_POINT_COMMAND not found")
    api_command = copy.copy(settings.VM_END_POINT_COMMAND)
    api_command.append(json_object['command'])
    api_command.append("'%s'" % json.dumps(json_object['parameters']))
    try:
        response = subprocess.check_output(api_command, stderr=subprocess.STDOUT)
        LOGGER.info("VM API request: %s\nVM API response: %s", api_command, response)
    except subprocess.CalledProcessError as e:
        LOGGER.error("VM API request: %s\nVM API response: %s", api_command, e.output)
        raise VMAPIFailure()
    return response


class XenWithFailure(Task):
    abstract = True
    def on_failure(self, exc, task_id, args, kwargs, einfo):
        if type(exc) is subprocess.CalledProcessError:
            LOGGER.error("An error happened when trying to communicate with Xen's VM API.\nThe task id is %s.\n\n"
                         "The parameters passed to the task were: %s\n\nThe traceback is:\n%s\n\n"
                         "The output from the command was: %s\n", task_id, args, einfo, exc.output)
        else:
            LOGGER.error("An error happened when trying to communicate with Xen's VM API.\nThe task id is %s.\n\n"
                         "The parameters passed to the task were: %s\n\n The traceback is: \n %s", task_id, args, einfo)


def secrets_prealocation(vm):
    # Gets all the keys generated for the site and generates the fingerprint and the SSHFP from them
    # It sends the SSHFP record to ip-register
    service = vm.service

    for keytype in ["sshrsa", "sshdsa", "sshecdsa", "sshed25519"]:
        p = subprocess.Popen(["userv", "mws-admin", "mws_pubkey"], stdin=subprocess.PIPE,
                             stdout=subprocess.PIPE, stderr=subprocess.PIPE)
        stdout, stderr = p.communicate(json.dumps({"id": "mwssite-%d" % service.site.id, "keytype": keytype}))
        try:
            result = json.loads(stdout)
        except ValueError as e:
            LOGGER.error("mws_pubkey response is not properly formated:\nstdout: %s\nstderr: %s" % (stdout, stderr))
            raise e

        pubkey = tempfile.NamedTemporaryFile()
        pubkey.write(result["pubkey"])
        pubkey.flush()
        fingerprint = subprocess.check_output(["ssh-keygen", "-lf", pubkey.name])

        SiteKey.objects.create(site=service.site, type=keytype.replace("ssh", "").upper(), public_key=result["pubkey"],
                                fingerprint=re.search("([0-9a-f]{2}:)+[0-9a-f]{2}", fingerprint).group(0))

        if keytype is not "sshed25519":  # "sshed25519" as of 2016 is not supported by jackdaw
            sshkeygeno = subprocess.check_output(["ssh-keygen", "-r", "replacehostname", "-f", pubkey.name]).split('\n')
            for i in [0, 1]:
                sshkglnout = sshkeygeno[i].split(' ')
                try:
                    set_sshfp(service.network_configuration.name, int(sshkglnout[3]), int(sshkglnout[4]), sshkglnout[5])
                    set_sshfp(service.site.test_service.network_configuration.name, int(sshkglnout[3]),
                              int(sshkglnout[4]), sshkglnout[5])
                    set_sshfp(vm.network_configuration.name, int(sshkglnout[3]), int(sshkglnout[4]), sshkglnout[5])
                except Exception as e:
                    pass
        pubkey.close()


@shared_task(base=XenWithFailure)
def new_site_primary_vm(service, host_network_configuration=None):
    parameters = {}
    parameters["site-id"] = "mwssite-%d" % service.site.id
    if getattr(settings, 'OS_VERSION_VMXENAPI', False):
        parameters["os"] = settings.OS_VERSION_VMXENAPI

    if host_network_configuration:
        netconf = {}
        if host_network_configuration.IPv4:
            netconf["IPv4"] = host_network_configuration.IPv4
        if host_network_configuration.IPv6:
            netconf["IPv6"] = host_network_configuration.IPv6
        if host_network_configuration.name:
            netconf["hostname"] = host_network_configuration.name
        vm = VirtualMachine.objects.create(service=service, token=uuid.uuid4(),
                                           network_configuration=host_network_configuration, cluster=which_cluster())
    else:
        raise AttributeError("No host network configuration")

    parameters["netconf"] = netconf
    parameters["callback"] = {
        "endpoint": "%s%s" % (settings.MAIN_DOMAIN, reverse(post_installation)),
        "vm_id": vm.id,
        "secret": str(vm.token),
    }

    service.status = 'installing'
    service.save()

    response = vm_api_request(command='create', parameters=parameters)

    try:
        jresponse = json.loads(response)
    except ValueError as e:
        LOGGER.error("VM API response is not properly formated: %s", response)
        vm.name = vm.network_configuration.name
        vm.save()
        raise e

    try:
        if 'vmid' in jresponse:
            vm.name = jresponse['vmid']
        else:
            vm.name = vm.network_configuration.name
    except Exception as e:
        vm.name = vm.network_configuration.name
    vm.save()
    from apimws.models import AnsibleConfiguration
    AnsibleConfiguration.objects.update_or_create(service=service, key='os',
                                                  defaults={'value': getattr(settings,
                                                                             "OS_VERSION_VMXENAPI", "jessie")})

    # Create a default Vhost and associate the service name
    default_vhost = Vhost.objects.create(service=service, name="default")
    service_domain = DomainName.objects.create(name=default_vhost.service.network_configuration.name,
                                               status="accepted", vhost=default_vhost)
    default_vhost.main_domain = service_domain
    default_vhost.save()
    secrets_prealocation(vm)


def recreate_vm(vm_id):
    vm = VirtualMachine.objects.get(pk=vm_id)
    service = vm.service
    network_configuration = vm.network_configuration
    parameters = {}
    parameters["site-id"] = "mwssite-%d" % service.site.id
    netconf = {}
    if network_configuration.IPv4:
        netconf["IPv4"] = network_configuration.IPv4
    if network_configuration.IPv6:
        netconf["IPv6"] = network_configuration.IPv6
    if network_configuration.name:
        netconf["hostname"] = network_configuration.name
    os = vm.service.ansible_configuration.filter(key='os')
    if os:
        parameters["os"] = os[0].value
    parameters["netconf"] = netconf
    parameters["callback"] = {
        "endpoint": "%s%s" % (settings.MAIN_DOMAIN, reverse(post_recreate)),
        "vm_id": vm.id,
        "secret": str(vm.token),
    }

    service.status = 'installing'
    service.save()

    response = vm_api_request(command='create', parameters=parameters)

    try:
        jresponse = json.loads(response)
    except ValueError as e:
        LOGGER.error("VM API response is not properly formated: %s", response)
        vm.name = vm.network_configuration.name
        vm.save()
        raise e

    try:
        if 'vmid' in jresponse:
            vm.name = jresponse['vmid']
        else:
            vm.name = vm.network_configuration.name
    except Exception as e:
        vm.name = vm.network_configuration.name
    vm.save()


@shared_task(base=XenWithFailure)
def change_vm_power_state(vm_id, on):
    if on != 'on' and on != 'off':
        raise VMAPIInputException("passed wrong parameter power %s" % on)
    vm = VirtualMachine.objects.get(pk=vm_id)
    lock = filter(lambda x: x and x['name'] == u'apimws.xen.change_vm_power_state' and
                            x['args'] == u"(%s, '%s')" % (vm_id, on),
                  [item for sublist in app.control.inspect().active().values() for item in sublist])
    if len(lock) == 1:
        vm_api_request(command='button', parameters={"action": "power%s" % on, "vmid": vm.name})
        return True
    else:
        return False


@shared_task(base=XenWithFailure)
def reset_vm(vm_id):
    lock = filter(lambda x: x and x['name'] == u'apimws.xen.reset_vm' and x['args'] == u'(%s,)' % vm_id,
                  [item for sublist in app.control.inspect().active().values() for item in sublist])
    if len(lock) == 1:
        vm_api_request(command='button',
                       parameters={"action": "reboot", "vmid": VirtualMachine.objects.get(pk=vm_id).name})
        return True
    else:
        return False


@shared_task(base=XenWithFailure)
def destroy_vm(vm_id):
    vm_api_request(command='delete', parameters={'vmid': VirtualMachine.objects.get(pk=vm_id).name})
    return True


def clone_vm(site, primary_vm):
    if primary_vm:
        original_service = site.production_service
        delete_service = site.test_service
    else:
        original_service = site.test_serivce
        delete_service = site.production_service

    if not delete_service:
        raise Exception("A site has no production or test service")  # TODO create custom exception

    # TODO restore this service in case the clonning does not work? then do not delete the VMs

    service_netconf = delete_service.network_configuration
    service_type = delete_service.type
    delete_service.delete()

    destination_service = Service.objects.create(site=original_service.site, type=service_type,
                                                 network_configuration=service_netconf, status='requested')
    destination_vm = VirtualMachine.objects.create(token=uuid.uuid4(), service=destination_service,
                                                   network_configuration=NetworkConfig.get_free_host_config(),
                                                   cluster=which_cluster())

    clone_vm_api_call.delay(original_service, destination_vm)


@shared_task(base=XenWithFailure)
def clone_vm_api_call(original_service, destination_vm):

    original_vm = original_service.virtual_machines.first()
    destination_service = destination_vm.service

    parameters = {}
    parameters["netconf"] = {}
    if destination_vm.network_configuration.IPv4:
        parameters["netconf"]["IPv4"] = destination_vm.network_configuration.IPv4
    if destination_vm.network_configuration.IPv6:
        parameters["netconf"]["IPv6"] = destination_vm.network_configuration.IPv6
    if destination_vm.network_configuration.name:
        parameters["netconf"]["hostname"] = destination_vm.network_configuration.name

    response = vm_api_request(command='clone', vmid=original_vm.name, parameters=parameters)

    response = json.loads(response)

    if 'vmid' in response:
        destination_vm.name = response['vmid']
    else:
        destination_vm.name = destination_vm.network_configuration.name
    destination_vm.save()

    destination_service.status = 'ready'
    destination_service.save()

    # Copy Unix Groups
    for unix_group in original_service.unix_groups.all():
        copy_users = unix_group.users.all()
        unix_group.pk = None
        unix_group.service = destination_service
        unix_group.save()
        unix_group.users = copy_users

    # Copy Ansible Configuration
    for ansible_conf in original_service.ansible_configuration.all():
        ansible_conf.pk = None
        ansible_conf.service = destination_service
        ansible_conf.save()

    # Copy vhosts
    # TODO copy Domain Names
    for vhost in original_service.vhosts.all():
        vhost.pk = None
        vhost.main_domain = None
        vhost.service = destination_service
        vhost.save()

    return True


def which_cluster():
    """This function decides which cluster to use when creating a new VM based on how busy each cluster is.
    At present we only have one cluster, so the decision algorithm is pretty obvious. Returns a Cluster object.
    """
    return Cluster.objects.first()
