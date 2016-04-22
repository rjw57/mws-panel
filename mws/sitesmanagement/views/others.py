"""Views(Controllers) for other purposes not in other files"""

import bisect
import json
from django.conf import settings
from django.contrib import messages
from django.contrib.auth.decorators import login_required, user_passes_test
from django.core.urlresolvers import reverse
from django.db import transaction
from django.http import HttpResponseRedirect, HttpResponseForbidden, HttpResponse, HttpResponseNotFound
from django.shortcuts import render, get_object_or_404, redirect
from django.utils.encoding import smart_str
from apimws.ansible import launch_ansible, ansible_change_mysql_root_pwd
from apimws.models import AnsibleConfiguration
from apimws.vm import clone_vm_api_call
from mwsauth.utils import privileges_check
from sitesmanagement.forms import BillingForm
from sitesmanagement.utils import get_object_or_None
from sitesmanagement.models import Service, Billing, Site, NetworkConfig, DomainName
from sitesmanagement.views.sites import warning_messages


@login_required
def billing_management(request, site_id):
    site = privileges_check(site_id, request.user)

    if site is None:
        return HttpResponseForbidden()

    breadcrumbs = {
        0: dict(name='Managed Web Service server: ' + str(site.name), url=site.get_absolute_url()),
        1: dict(name='Billing', url=reverse('billing_management', kwargs={'site_id': site.id}))
    }

    if request.method == 'POST':
        if hasattr(site, 'billing'):
            billing_form = BillingForm(request.POST, request.FILES, instance=site.billing)
            if billing_form.is_valid():
                billing_form.save()
                return redirect(site)
        else:
            billing_form = BillingForm(request.POST, request.FILES)
            if billing_form.is_valid():
                billing = billing_form.save(commit=False)
                billing.site = site
                billing.save()
                return redirect(site)
    elif hasattr(site, 'billing'):
        billing_form = BillingForm(instance=site.billing)
    else:
        billing_form = BillingForm()

    return render(request, 'mws/billing.html', {
        'breadcrumbs': breadcrumbs,
        'site': site,
        'billing_form': billing_form,
        'sidebar_messages': warning_messages(site),
        'cost': settings.YEAR_COST
    })


@login_required
def clone_vm_view(request, site_id):
    site = privileges_check(site_id, request.user)

    if site is None:
        return HttpResponseForbidden()

    breadcrumbs = {
        0: dict(name='Managed Web Service server: ' + str(site.name), url=site.get_absolute_url()),
        1: dict(name='Production and test servers management', url=reverse(clone_vm_view, kwargs={'site_id': site.id}))
    }

    if request.method == 'POST' and site.is_ready and site.test_service:
        clone_vm_api_call.delay(site)
        return redirect(site)

    return render(request, 'mws/clone_vm.html', {
        'breadcrumbs': breadcrumbs,
        'site': site,
    })


def privacy(request):
    return render(request, 'privacy.html', {})


def termsconds(request):
    return render(request, 'tcs.html', {})


@login_required
def service_status(request, service_id):
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    if service.is_ready:
        return HttpResponse(json.dumps({'status': 'ready'}), content_type='application/json')
    else:
        return HttpResponse(json.dumps({'status': 'busy'}), content_type='application/json')


@login_required
def service_settings(request, service_id):
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    if service.is_busy:
        return redirect(site)

    breadcrumbs = {
        0: dict(name='Managed Web Service server: ' + str(site.name), url=site.get_absolute_url()),
        1: dict(name='Server settings' if service.primary else 'Test server settings',
                url=reverse(service_settings, kwargs={'service_id': service.id}))
    }

    return render(request, 'mws/settings.html', {
        'breadcrumbs': breadcrumbs,
        'site': site,
        'service': service,
        'sidebar_messages': warning_messages(site),
    })


@login_required
def system_packages(request, service_id):
    if getattr(settings, 'DEMO', False):
        return HttpResponseRedirect(reverse('listsites'))
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    if not service or not service.active or service.is_busy:
        return redirect(site)

    ansible_configuraton = get_object_or_None(AnsibleConfiguration, service=service, key="system_packages") \
                           or AnsibleConfiguration.objects.create(service=service, key="system_packages", value="")

    packages_installed = list(int(x) for x in ansible_configuraton.value.split(",")) \
        if ansible_configuraton.value != '' else []

    breadcrumbs = {
        0: dict(name='Managed Web Service server: ' + str(site.name), url=site.get_absolute_url()),
        1: dict(name='Server settings' if service.primary else 'Test server settings',
                url=reverse(service_settings, kwargs={'service_id': service.id})),
        2: dict(name='System packages', url=reverse(system_packages, kwargs={'service_id': service.id}))
    }

    package_number_list = [1, 2, 3, 4]  # TODO extract this to settings

    if request.method == 'POST':
        package_number = int(request.POST['package_number'])
        if package_number in package_number_list:
            if package_number in packages_installed:
                packages_installed.remove(package_number)
                ansible_configuraton.value = ",".join(str(x) for x in packages_installed)
                ansible_configuraton.save()
            else:
                bisect.insort_left(packages_installed, package_number)
                ansible_configuraton.value = ",".join(str(x) for x in packages_installed)
                ansible_configuraton.save()

            launch_ansible(service)  # to install or delete new/old packages selected by the user

    return render(request, 'mws/system_packages.html', {
        'breadcrumbs': breadcrumbs,
        'packages_installed': packages_installed,
        'site': site,
        'sidebar_messages': warning_messages(site),
        'service': service
    })


@login_required
def delete_vm(request, service_id):
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None or service.primary:
        return HttpResponseForbidden()

    if not service or not service.active or service.is_busy:
        return redirect(site)

    if request.method == 'DELETE':
        for vm in service.virtual_machines.all():
            vm.delete()
        return redirect(site)

    return HttpResponseForbidden()


@login_required
def power_vm(request, service_id):
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    if not service or not service.active or service.is_busy:
        return redirect(site)

    service.power_on()

    return redirect(service_settings, service_id=service.id)


@login_required
def reset_vm(request, service_id):
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    if not service or not service.active or service.is_busy:
        return redirect(site)

    if request.method == 'POST':
        if service.do_reset():
            messages.success(request, "Your site will be restarted shortly")
        else:
            messages.error(request, "Your site couldn't be restarted")

    return redirect(service_settings, service_id=service.id)


@login_required
def update_os(request, service_id):
    if getattr(settings, 'DEMO', False):
        return HttpResponseRedirect(reverse('listsites'))
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    if not service or not service.active or service.is_busy:
    # TODO change the button format (disabled) if the vm is not ready
        return redirect(site)

    # TODO 1) Warn about the secondary VM if exists
    # TODO 2) Delete secondary VM if exists
    # TODO 3) Create a new VM with the new OS and launch an ansible task to restore the state of the DB
    # TODO 4) Put it as a secondary VM?

    return HttpResponse('')


@login_required
def change_db_root_password(request, service_id):
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    if not service or not service.active or service.is_busy:
        return redirect(site)

    breadcrumbs = {
        0: dict(name='Managed Web Service server: ' + str(site.name), url=site.get_absolute_url()),
        1: dict(name='Server settings' if service.primary else 'Test server settings',
                url=reverse(service_settings, kwargs={'service_id': service.id})),
        2: dict(name='Change db root pass', url=reverse(change_db_root_password, kwargs={'service_id': service.id})),
    }

    if request.method == 'POST':
        if request.POST.get('typepost') == "Delete temporal mySQL root password":
            AnsibleConfiguration.objects.filter(service=service, key="mysql_root_password").delete()
        else:
            ansibleconf, created = AnsibleConfiguration.objects.get_or_create(service=service,
                                                                              key="mysql_root_password")
            ansibleconf.value = "Resetting"
            ansibleconf.save()
            ansible_change_mysql_root_pwd.delay(service)
            return HttpResponseRedirect(reverse(change_db_root_password, kwargs={'service_id': service.id}))

    ansibleconf = AnsibleConfiguration.objects.filter(service=service, key="mysql_root_password")
    ansibleconf = ansibleconf[0].value if ansibleconf else None

    return render(request, 'mws/change_db_root_password.html', {
        'breadcrumbs': breadcrumbs,
        'service': service,
        'site': site,
        'sidebar_messages': warning_messages(site),
        'ansibleconf': ansibleconf,
    })


@login_required
def apache_modules(request, service_id):
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    if not service or not service.active or service.is_busy:
        return redirect(site)

    breadcrumbs = {
        0: dict(name='Managed Web Service server: ' + str(site.name), url=site.get_absolute_url()),
        1: dict(name='Server settings' if service.primary else 'Test server settings',
                url=reverse(service_settings, kwargs={'service_id': service.id})),
        2: dict(name='Apache modules', url=reverse(apache_modules, kwargs={'service_id': service.id})),
    }

    from apimws.forms import ApacheModuleForm

    parameters = {
        'breadcrumbs': breadcrumbs,
        'service': service,
        'site': site,
        'sidebar_messages': warning_messages(site),
        'form': ApacheModuleForm(initial={'apache_modules': service.apache_modules.values_list('name', flat=True)}),
    }

    if request.method == 'POST':
        f = ApacheModuleForm(request.POST)
        if f.is_valid():
            service.apache_modules = f.cleaned_data['apache_modules']
            service.save()
            launch_ansible(service)

    return render(request, 'mws/apache.html', parameters)


@login_required
def php_libs(request, service_id):
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    if not service or not service.active or service.is_busy:
        return redirect(site)

    breadcrumbs = {
        0: dict(name='Managed Web Service server: ' + str(site.name), url=site.get_absolute_url()),
        1: dict(name='Server settings' if service.primary else 'Test server settings',
                url=reverse(service_settings, kwargs={'service_id': service.id})),
        2: dict(name='PHP Libraries', url=reverse(php_libs, kwargs={'service_id': service.id})),
    }

    from apimws.forms import PHPLibForm

    parameters = {
        'breadcrumbs': breadcrumbs,
        'service': service,
        'site': site,
        'sidebar_messages': warning_messages(site),
        'form': PHPLibForm(initial={'php_libs': service.php_libs.values_list('name', flat=True)}),
    }

    if request.method == 'POST':
        f = PHPLibForm(request.POST)
        if f.is_valid():
            service.php_libs = f.cleaned_data['php_libs']
            service.save()
            launch_ansible(service)

    return render(request, 'mws/phplibs.html', parameters)


def po_file_serve(request, filename):
    billing = Billing.objects.filter(purchase_order='billing/%s' % filename)
    if billing.exists() and ((request.user in billing[0].site.list_of_admins()) or request.user.is_superuser):
        pofile = billing[0].purchase_order
        response = HttpResponse(pofile.read(), content_type='application/zip')
        response['Content-Disposition'] = 'attachment; filename=%s' % smart_str(filename)
        response['Content-Length'] = pofile.tell()
        return response
    else:
        return HttpResponseNotFound()


@login_required
def quarantine(request, service_id):
    service = get_object_or_404(Service, pk=service_id)
    site = privileges_check(service.site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    if not service or not service.active or service.is_busy:
        return redirect(site)

    breadcrumbs = {
        0: dict(name='Managed Web Service server: ' + str(site.name), url=site.get_absolute_url()),
        1: dict(name='Server settings' if service.primary else 'Test server settings',
                url=reverse(service_settings, kwargs={'service_id': service.id})),
        2: dict(name='Quarantine', url=reverse(quarantine, kwargs={'service_id': service.id})),
    }

    parameters = {
        'breadcrumbs': breadcrumbs,
        'service': service,
        'site': site,
        'sidebar_messages': warning_messages(site),
    }

    if request.method == 'POST' and not site.is_admin_suspended():
        if request.POST['quarantine'] == "Quarantine":
            service.quarantined = True
        else:
            service.quarantined = False
        service.save()
        launch_ansible(service)
        return redirect(site)

    return render(request, 'mws/quarantine.html', parameters)


@login_required
@user_passes_test(lambda u: u.is_superuser)
def admin_email_list(request):
    return render(request, 'mws/admin/email_list.html',
                  {'site_list': Site.objects.filter(deleted=False, end_date__isnull=True)})


@login_required
def switch_services(request, site_id):
    site = get_object_or_404(Site, pk=site_id)
    site = privileges_check(site.id, request.user)

    if site is None:
        return HttpResponseForbidden()

    with transaction.atomic():
        prod_service = site.production_service
        test_service = site.test_service
        netconf_prod = prod_service.network_configuration
        netconf_test = test_service.network_configuration
        test_service.network_configuration = NetworkConfig.get_free_test_service_config()
        test_service.type = "production"
        test_service.site = None
        test_service.save()
        prod_service.network_configuration = netconf_test
        prod_service.type = "test"
        prod_service.save()
        test_service.site = site
        test_service.network_configuration = netconf_prod
        test_service.save()

        dnt = DomainName.objects.get(name=test_service.network_configuration.name)
        dnp = DomainName.objects.get(name=prod_service.network_configuration.name)
        vhostt = dnt.vhost
        vhostp = dnp.vhost
        dnt.vhost = vhostp
        dnt.save()
        dnp.vhost = vhostt
        dnp.save()
        if vhostt.main_domain == dnt:
            vhostt.main_domain = dnp
        if vhostp.main_domain == dnp:
            vhostp.main_domain = dnt

        launch_ansible(prod_service)
        launch_ansible(test_service)

    return redirect(site)
