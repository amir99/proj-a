# -*- coding: utf-8 -*-
from django.http import Http404

from product.models import *
from product.product_util import get_price_list
from .forms import *
from customer.models import *
from farm.models import Farm
from order.models import *
from order.order_utils import general_style, strong_style, warning_style, \
        get_basket_cache, get_route_cache, get_current_orders
from payment.models import *
from payment.utils import get_balance
from order.lists import _orders_data
from core.utils import render_as_csv, unicode_csv_reader, mail_superadmin, get_customer_cache
from cart.models import Cart
from customer.send_sms import send_sms
from route.routing import geocode_address, AddressNotFoundException
from supplier.models import Supplier
from pages.models import Page, Content
from constance import config

from django.conf import settings
from django.http import Http404, HttpResponse, HttpResponseRedirect
from django.shortcuts import render,  get_object_or_404, redirect
from django.template import RequestContext
from django.template.loader import render_to_string
from django.urls import reverse
from django.db.models import Sum, Count, F
from django.contrib import messages
from django.utils import timezone
from django.contrib.admin.models import LogEntry
from django.utils import formats
from django.contrib.auth.models import User, Group
from django.db import connection, connections
from users.models import User as NUser

from datetime import datetime, date, timedelta

from dateutil.parser import parse
from decimal import Decimal, ROUND_HALF_EVEN
from xlrd import open_workbook
from collections import OrderedDict
import os
import urllib
import random
import csv
import logging
import pathlib

def get_product_cache():
    p_cache = {}
    products = Product.objects.select_related('supplier', 'category').all()
    for p in products:
        p_cache[p.name] = p
    return p_cache

import xlwt
from xlwt import Utils as xlutil
from xlwt import Workbook,easyxf

def render_as_table(rows):
    table_outer = '<table class="table">{}</table>'
    row_tpl = '<tr>{}</tr>'
    col_tpl = '<td>{}</td>'
    inner_str = ''
    for r in rows:
        row_str = ''
        for cell in r:
            row_str += col_tpl.format(cell)
        inner_str += row_tpl.format(row_str)
    return table_outer.format(inner_str)


def baskets_report(request):
    """ a report containing ordered sums per basket, and per all the basket's components """

    if request.GET.get('sel', None):
        oids = get_current_orders(route_ids='all', all_routes=None, order_ids_only=True)
        pid = int(request.GET.get('sel'))
        op = OrderProduct.objects.select_related('order').filter(product_id=pid, order_id__in=oids)
        cc = get_customer_cache(key='user_id')
        res = []
        for item in op:
            has_selection = True
            try:
                s = item.data['selection']
                if not s:
                    has_selection = False
            except KeyError:
                has_selection = False
            res.append((cc[item.order.user_id].get_full_name(),
                        has_selection,))
        return render_as_csv(res, 'לקוחות שהזמינו סלים עם בחירה', 'baskets_with_selection.csv') 
    else:
        baskets_without_rep = BasketDesc.objects.exclude(basket_list='') \
                                                .exclude(basket_list__contains='[') \
                                                .count()
        return render(request, 'admin_layout.html', {
              'content': u'<h3>מספר סלים ללא החלפות: ' + str(baskets_without_rep) + '</h3>'
         })


def payments_report(request, month, year):
    from_date = date(int(year), int(month), 1)
    month_payments = PaymentRecord.objects.filter(date__year=year, date__month=month) \
            .order_by('pk')

    if not month_payments.exists():
        return HttpResponse('<dir style="color:red; font-size:30; margin: 5em 8em;">No records found for the requested period.</dir>')
    filename = "payment_report_%s_%s.xls" % (month, year,)
    book = Workbook(encoding='windows-1255')
    sheet = book.add_sheet('Sheet 1',cell_overwrite_ok=True)
    sheet.print_grid = True
    sheet.show_grid = True
    sheet.cols_right_to_left = True
    sheet.portrait = False
    # sheet.col(0).width = 36 * 256

    row_num = 0
    sheet.row(0).write(0, u'תאריך', strong_style)
    sheet.row(0).write(1, u'שם לקוח', strong_style)
    sheet.row(0).write(2, u'סכום', strong_style)
    sheet.row(0).write(3, u'סכום חייב במעמ', strong_style)
    sheet.row(0).write(4, u'סוג תשלום', strong_style)
    sheet.row(0).write(5, u'הערה', strong_style)
    row_num += 1
    for payment in month_payments:
        sheet.row(row_num).write(0, formats.date_format(payment.date, "SHORT_DATE_FORMAT"), general_style)
        sheet.row(row_num).write(1, payment.customer.get_full_name(), general_style)
        sheet.row(row_num).write(2, payment.amount, general_style)
        sheet.row(row_num).write(3,  payment.taxable_amount, general_style)
        sheet.row(row_num).write(4,  payment.get_type_display(), general_style)
        sheet.row(row_num).write(5,  payment.note, general_style)
        row_num += 1


    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response['Content-Disposition'] = 'attachment; filename=%s' % (filename,)
    book.save(response)
    return response


from route.models import Route

def _get_routes_cache():
    routes_cache = {}
    routes = Route.objects.all()
    for r in routes:
        routes_cache[r.id] = r.cities.values_list('id', flat=True)
    return routes_cache

"""
def get_customers_with_wrong_route(request):
    rc = _get_routes_cache()
    max_id = request.GET.get('max_id', None)
    if not max_id:
        return HttpResponse('--- no max id ---')
    res = []
    min_id = int(max_id) - 500
    customers = Customer.objects.filter(id__gt=min_id, id__lte=max_id).order_by('id')
    for c in customers[:500]:
        if c.route and c.city_id not in rc[c.route_id]:
            if c.user.is_active and not c.pickup_point:
                res.append(c.get_full_name())
            # else
            #    res.append('0,' + c.get_full_name())
    return HttpResponse('<br/>'.join(res))
"""

def discard_old_balance(request):
    date_str = request.GET.get('d', None)
    sch = request.tenant.schema_name
    date = datetime.strptime(date_str, '%Y-%m-%d').date()
    limit = int(request.GET.get('limit', '300'))
    # start_from_id = request.GET.get('start_id', None)
    is_test = request.GET.get('test', None)
    from_id_obj = LogEntry.objects.filter(object_repr='discard_old_balance').order_by('id')
    if from_id_obj.exists():
        ln = from_id_obj.count()
        from_id = int(from_id_obj[ln-1].object_id) + 1
    else:
        from_id = 1

    customers = Customer.objects.filter(id__gte=int(from_id)).order_by('id')
    res = []
    last_id = 0
    media_path = '{0}/data/{1}'.format(settings.MEDIA_ROOT, sch)
    d_o = open('{0}/discarded_orders_{1}.csv'.format(media_path, from_id), mode='w')
    d_p = open('{0}/discarded_payments_{1}.csv'.format(media_path, from_id), mode='w')
    d_d = open('{0}/discarded_debits_{1}.csv'.format(media_path, from_id), mode='w')
    o_writer = csv.writer(d_o, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    p_writer = csv.writer(d_p, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)
    d_writer = csv.writer(d_d, delimiter=',', quotechar='"', quoting=csv.QUOTE_MINIMAL)

    for c in customers[:limit]:
        # calculate current balance
        last_id = c.id
        cur_balance = get_balance(c)
        total, taxable = 0, 0
        orders = Order.shipped.for_customer(c).filter(date__lt=date, date__isnull=False) \
                                                .order_by('date')
        payments = c.payments.filter(date__lt=date)
        # get all debit/refund records(taxable and non-taxabla)
        debit_r_total = c.refunds_debits.filter(date__lt=date)
        # get only taxable debitqrefund records
        debit_r_taxed = c.refunds_debits.filter(date__lt=date,taxable=True)

        if not orders.exists() or orders[0].date > date:
            continue

        orders_sum = orders.aggregate(sum=Sum('sum'), taxable=Sum('taxable_sum'))
        pay_sum = payments.aggregate(sum=Sum('amount'), taxable=Sum('taxable_amount'))
        debit_refund_total = debit_r_total.aggregate(sum=Sum('amount'))
        debit_refund_tax = debit_r_taxed.aggregate(sum=Sum('amount'))

        if is_test:
            continue
        
        for o in orders:
            o_writer.writerow([c.id, o.id, o.sum, o.taxable_sum, o.date])
        for p in payments:
            p_writer.writerow([c.id, c.get_full_name(), p.id, p.amount, p.taxable_amount, p.date])
        for d in debit_r_total:
            d_writer.writerow([c.id, c.get_full_name(), d.id, d.amount, d.taxable, d.date])
        orders.delete()
        payments.delete()
        debit_r_total.delete()

        total = c.balance_start_point
        total += (orders_sum['sum'] or 0)
        total -= (pay_sum['sum'] or 0)
        total += (debit_refund_total['sum'] or 0)
        taxable = c.taxable_balance_start_point
        taxable += orders_sum['taxable'] or 0
        taxable -= (pay_sum['taxable'] or 0)
        taxable += (debit_refund_tax['sum'] or 0)

        c.balance_start_point = total
        c.taxable_balance_start_point = taxable
        c.save()
        balance_after_change = get_balance(c)
        if cur_balance['total'] != balance_after_change['total']:
            res.append(c.get_full_name() + ' ' + str(c.id) + ' [' + str(cur_balance['total']) + 
                       ' , ' + str(balance_after_change['total']) + '<br/>')
        else:
            res.append('.') # c.get_full_name() + ' ' + str(c.id))     


    LogEntry.objects.create(user_id=request.user.id,
                    object_repr='discard_old_balance', object_id=last_id, change_message='', action_flag=1)

    return HttpResponse('===== Done ===== ' + ' '.join(res))

"""
def fix_tax_field(request):
    p = Product.objects.filter(tax__taxrate__gt=0)
    p.update(taxrate=17)
    p = Product.objects.filter(tax__taxrate=0)
    p.update(taxrate=0)
    return HttpResponse('=== Ok ===')
"""

def _has_item(item, cur_items):
    for cur_it in cur_items:
        if cur_it.id == item[0] or cur_it.name == item[1]:
            return True
    return False
    

import csv
import os

def check_orders_for_client(request, uid):
    user = get_object_or_404(User, id=int(uid))
    from_date = date.today() - timedelta(days=15)
    customer = user.profile
    html = '''<div style="text-align: left; direction: ltr;">
            <div><h2 class="text-align: center">Orders Info For: {0}</h2></div>
            <h2 class="float-left">Carts</h2>{1}
            <h2 class="float-left">orders</h2>{2}</div>'''
    carts = []
    orders = []
    recent_orders = Order.objects.filter(user_id=int(uid), registration_date__gte=from_date)
    recent_carts = Cart.objects.filter(user_id=int(uid), created__gte=from_date)
    if recent_orders.exists():
        for ordr in recent_orders:
            o_arr = [str(ordr.id), str(ordr.date), ordr.status, str(ordr.registration_date)[:-13],
                     str(ordr.last_edited)[:-13], 'by uid: ' + str(ordr.registered_by_id),
                     str(ordr.sum), ' cart id: ' + str(ordr.cart_id) if ordr.cart else '-',]
            orders.append(o_arr)

    if recent_carts.exists():
        for crt in recent_carts:
            c_arr = [str(crt.id), str(crt.created)[:-13], str(crt.last_change)[:-13],
                     'PERM' if crt.is_perm else 'REG', '[' + str(crt.quantity) + ']',
                     crt.note, crt.discount_name, str(crt.discount_amount),
                     str(crt.shipping_discount), 
                     crt.pickup_point.title if crt.pickup_point else 'HOME',
                     'Closed' if crt.closed else ' ',   
                     ] 
            carts.append(c_arr)
    return render(request, 'admin_layout.html', {
                                     'content': html.format(customer.get_full_name(),
                                                            render_as_table(carts),
                                                            render_as_table(orders)),
                                  })


def no_mail(request): 
    filename = 'data/{0}/no_mail.csv'.format(request.tenant.schema_name)
    cnt = 0;
    data = unicode_csv_reader(open(os.path.join(settings.MEDIA_ROOT,filename)))
    for row in data:
        try:
            c = Customer.objects.get(first_name=row[0], last_name=row[1])
        except:
            print(row, '... not found')

        else:
            c_pref, created = CustomerProp.objects.get_or_create(customer=c, prop_name='email_level', defaults={'prop_value': 0})
            if not created:
                c_pref.prop_value = 0
                c_pref.save()
            cnt += 1
    return HttpResponse('================== Done [{0}] ================='.format(cnt) )


def misc(request):
    action = request.GET.get('a', '')
    res = []
    if action == 'no-token':
        filename = 'data/{0}/payed.csv'.format(request.tenant.schema_name)
        cnt = 0;

        data = unicode_csv_reader(open(os.path.join(settings.MEDIA_ROOT,filename)))
        for row in data:
            try:
                c = Customer.objects.get(first_name=row[0], last_name=row[1])
            except:
                print(row, '... not found')

            else:
                if not c.get_valid_token():
                    res.append(c.get_full_name())
    elif action == 'changed_baskets':
        routes_ids = urllib.parse.unquote_plus(request.COOKIES.get('routes', ''))
        oo = get_current_orders(route_ids=route_ids)
        no_change_count = 0
        # for o in oo:
    elif action == 'tax':
        from_date = date.today() - timedelta(days=6)
        orders = Order.objects.filter(date__gte=from_date, status='shipped')
        for o in orders:
            if o.discount_sum == 5 and o.status != 'canceled':
                t = o.products_taxable_amount() + o.delivery_price
                if t != o.taxable_sum:
                    print(o.id)
                    o.taxable_sum = t
                    o.save()
    elif action == 'unw':
        # check un-wanted products(kaima)
        # get customers with an order in the last 3 months
        customer_ids = []
        orders = Order.objects.select_related('user__profile').filter(
              date__gt=date.today()-timedelta(days=90), status='shipped')
        for o in orders:
            customer_ids.append(o.user.profile.id)
        prefs = ProductPref.objects.select_related('product').filter(customer_id__in=customer_ids,
                                           wanted=False,
                                           temporary=False) \
            .values('product__name') \
            .annotate(unwanted_count=Count('id'))
        prod = set()
        txt = '<div style="direction: rtl; font-family: arial;">'
        for p in prefs:
            print(p)
            if p['product__name'] not in prod:
                txt += '<h4>{}</h4>'.format(p['product__name'] + ': ' + str(p['unwanted_count']))
                prod.add(p['product__name'])
        return HttpResponse(txt + '</div>')

    return HttpResponse('<div>' + '<br/>'.join(res) + '</div>')


def create_customer(request, uid):
    from django.contrib.auth import login

    if int(uid) == 0:
        ltr = list('abcdefghijklmnopqrst23490')
        random.shuffle(ltr)
        eml = ''.join(ltr)[:8] + '@juju.co.il'
        u = User.objects.create(
            email = eml,
            is_active=True,
            is_staff=False,
            is_superuser=False,
            date_joined=datetime.today(),
            password='qwerty')
        uid = u.id
    try:
        random.shuffle(ltr)
        s = ''.join(ltr)

        customer = Customer.objects.create(user_id=int(uid),
                cell_phone='0501111111',
                home_phone='021111111',
                payment_method=1,
                first_name=s[:5],
                last_name=s[18:],
                city_id=32)
    except Exception as e:
        return HttpResponse('------------- ' + str(e))
    print(customer.get_full_name())
    if config._EXTRA_CUSTOMER_FORM:
        param = '?mail=' + u.email
        login(request, u)
        return HttpResponseRedirect(reverse('customer:custom-form', 
                                    kwargs={'lang': 'HE'}) + param) 
                    # return custom_form(request, customer.lang)
    return HttpResponse('--------- Ok -----------')


def get_set_farm_info(request, which_data, get_or_set):

    tenant = request.tenant
    if which_data == 'h':
        return HttpResponse('usage: /[s|l|c] / [g|s]  (s|l|c: s=settings, l=orderlist, c=city  g|s: get or set )' +
                            '<br/> ?setting=<new setting> with "^" instead of "#" : add or edit a setting ' +
                            '<br/> (if no setting parameter is set - show the entire settings string ' +
                            '<br/> ?list=? :  get all existing lists ' +
                            '<br/> ?list=list_internal_name : to show a list with this internal name' + 
                            '<br/> ?list=list_int_name^list_fields (with ! as separator between fields) : set a new list or edit an existing one' +
                            '<br/> (in case of a new list the parameter starts with the title annd not the internal name)'
                            '<br/> ?city=city : add a city ')
    elif which_data == 's':
        if get_or_set == 'g':
            return HttpResponse(tenant.settings)
        elif get_or_set == 's':
            data = request.GET.get('setting', '')
            if data:
                # print data
                sep_idx = data.index('^')
                key = data[:sep_idx]
                new_val = data[sep_idx+1:]
                val = tenant.get_setting(key)
                if val:
                    new_settings = tenant.settings.replace(key + '#' + val, key + '#' + new_val)
                    if new_val == '0':
                        to_be_replaced = key + '#' + val + ';'
                        if tenant.settings.index(to_be_replaced) == -1:
                            to_be_replaced = to_be_replaced[:-1]

                        new_settings = tenant.settings.replace(to_be_replaced, '')
                else:
                    new_settings = tenant.settings + ';' + data.replace('^', '#')
                sql = "update public.farm_farm set settings='{0}' where schema_name='{1}'".format(
                    new_settings, tenant.schema_name)
                cursor = connections['raw'].cursor()
                cursor.execute(sql)
                

    elif which_data == 'l':
        data = request.GET.get('list', '')
        sep_idx = data.find('^')
        data = data.replace('!', ';')
        key = data[:sep_idx] if sep_idx > -1 else data
        key = key.replace('%20', ' ')
        # print key

        try:
            lst = OrderList.objects.get(name_int=key)
        except OrderList.DoesNotExist:
            lst = None
        if get_or_set == 'g':
            if data == '?':
                lists = OrderList.objects.all()
                ret = ''
                for l in lists:
                    ret += l.name + '\t|\t' + l.name_int + \
                            '\t|\t\t <strong>fields:</strong>\t' + l.fields + ' <br/>'
                return HttpResponse(ret)
            elif lst:
                return HttpResponse(lst.fields)
            else:
                return HttpResponse('---- Not found ----')
        elif get_or_set == 's':
            # print '>>> ', data[sep_idx + 1:]
            if lst:
                lst.fields = data[sep_idx + 1:]
                lst.save()
            else:
                name_int = 'a_' + str(random.randint(30, 500))
                obj = OrderList(name=key, name_int=name_int, fields=data[sep_idx+1:])
                obj.save()
    elif which_data == 'c':
        city_name = request.GET.get('city', '')
        if city_name:
            c = City.objects.filter(name=city_name)
            if get_or_set == 'g':
                if c.count() == 1:
                    return HttpResponse('=== exists ===')
                elif c.count() > 1:
                    return HttpResponse('=== more than one exists ===')
                else:
                    return HttpResponse('=== Not found ===')
            elif get_or_set == 's' and not c.exists():
                last = City.objects.all().order_by('-id')[0]
                city_name = city_name.replace('%20', ' ')
                City.objects.create(id=last.id+1, name=city_name)
        else:
            return HttpResponse('--- No cities defined! ---')
    return HttpResponse('----------- Done -----------')


from product.product_util import order_the_produts
def sort_products(request, return_str=False):
    term = request.GET.get('t', 'shop')
    cat = int(request.GET.get('c', '0'))
    as_str = int(request.GET.get('str', '0'))
    if return_str:
        as_str = True
    products = Product.objects.filter(active=True)
    if cat == 0:
        category = Category.objects.all()[0]
        cat = category.id

    products = products.filter(category_id=cat)
    ordered_products = order_the_produts(list(products), term, 'p', False)
    plen = len(ordered_products)
    if as_str:
        return render_to_string('product/partials/sort_products.html', {
                            'product_list': ordered_products,
                            'categories': Category.objects.all(),
                            'sel_cat': cat,
                            'order_term': term,
                            'as_string': True,
                            }, request)
    else:
        return render(request, 'product/partials/sort_products.html', {
            'product_list': ordered_products,
            'categories': Category.objects.all(),
            'sel_cat': cat,
            'order_term': term,
        })



def sort_categories(request):
    term = request.GET.get('t', 'shop')
    cat = int(request.GET.get('c', '0'))
    if cat == 0:
        categories = Category.objects.all()
    else:
        categories = Category.objects.filter(parent_id=cat)
    """
    ordered_categories = order_the_categories(list(categories), term, 'p')
    plen = len(ordered_categories)
    # print plen
    return render(request, 'product/partials/sort_products.html', {
                            'product_list': ordered_categories,
                            'categories': Category.objects.filter(parent_id__isnull=True),
                            'sel_cat': cat,
                            'order_term': term
                                  })
    """


def close_todays_orders(request):
    the_date = datetime.today()
    plus = request.GET.get('plus_days', None)
    if plus:
        the_date = the_date + timedelta(days=int(plus))
    
        
    current_orders = order.models.Order.objects.filter(
                    approved=True, date=the_date.date()) \
                    .exclude(status='canceled')
                    
    # for o in route_current_orders:
    #    print o.id, o.date
    current_orders.update(status='shipped')
    return HttpResponse("========== Done ======= " + str(current_orders.count()))


from django.contrib.admin.models import LogEntry
def get_perm_log_entry(request):
    try:
        uid = int(request.GET.get('uid'))
    except TypeError:
        return HttpResponse('Please supply get parameter "?uid=<user_id>"')

    rows = []
    now = datetime.now() 
    ents = LogEntry.objects.filter(user_id=uid,
                                    action_time__gt=now-timedelta(days=180))
    
    header = '<tr><th>לקוח</th><th>פעולה</th><th>מתי?</th><th>ע"י מי?</th></tr>'

    for e in ents:
        txt = e.change_message
        idx = txt.find('by')
        rows.append('<tr><td>' + e.user.profile.get_full_name() + '</td><td>' + e.object_repr + '</td><td>' + txt[:idx-8] + '</td><td>' + txt[idx+3:] +  '</td></tr>')

    table_content = ''.join(rows)
    return render(request, 'admin_layout.html', {
                                     'content': '<table class="data">' + header + table_content + '</table>',
                                  })


# manage/extra/add_discount_by_product/?from=104570&pid=5000&&t=1&test=1
def add_discount_by_product(request):
    PROD_ID = int(request.GET.get('pid', '100100'))
    ord_date = request.GET.get('date', None)
    test_mode = request.GET.get('test', None)
    discount_sum = int(request.GET.get('sum', '0'))
    orders_with_prod = []
    
    if not date or PROD_ID == 100100:
        return HttpResponse('---------------- Missing params --------------- ' )
        
    the_date = parse(ord_date).date()
    product_to_check = get_object_or_404(Product, id=PROD_ID)

    orders = Order.objects.filter(date=the_date) \
            .prefetch_related('order_product_set')

    orders_to_fix = []
    po_objects = []
    oo = []
    print(orders.count())
    done_once = False
    for o in orders:
        if test_mode and done_once:
            break
        for op in o.order_product_set.all():
            if op.product_id == PROD_ID:
                print('>> ' + str(o.id))
                done_once = True
                if o.discount_sum != discount_sum and o.sum != 0:
                    o.discount_sum += discount_sum
                    o.sum -= discount_sum
                    o.save()   
                
    return HttpResponse('---------------- Done --------------- ' )
  


def fix_duplicate_products(request):
    res = []
    from_id = int(request.GET.get('from', '1'))
    to_id = int(request.GET.get('to', '5000000'))
    do_delete = int(request.GET.get('del', '0'))
    ord_prd = Order_Product.objects.filter(order__id__gte=from_id, order__id__lt=to_id) \
             .order_by('order__id', 'product__id', '-id')
    prev_ord = 0
    prev_prod = 0
    for item in ord_prd:
        if item.product_id == prev_prod and \
                        item.order_id == prev_ord:
            try:
                res.append('date: {0}, customer: {1}, order_id: {2}'.format(item.order.date, item.order.get_full_name(), item.order_id))
            except AttributeError:
                res.append('date: {0}, order_id: {1}'.format(item.order.date, item.order_id))
            if do_delete == 1:
                item.delete()
                item.order.recalculate_sums(None)
                print('item deleted!', prev_ord)
        prev_prod = item.product_id
        prev_ord = item.order_id

    res.append('============= Done ============')
    return HttpResponse('<br/>'.join(res))


def permanents_report(request):
    # date_inp = request.GET.get('date', '')
    # dt = datetime.strptime(date_inp, '%d-%m-%Y')
    dt = date.today()
    # prev_dt = date.today() - timedelta(days=7)
    rc = get_route_cache()
    this_week_getters = []
    last_week_getters = []
    this_week_getters.append(['', 'קיבלו הזמנה השבוע', '', '', '', ''])
    last_week_getters.append(['', 'קיבלו בשבוע שעבר', '', '', '', ''])
    logger = logging.getLogger( 'Replacements :: ' + __name__)
    biweekly_customers = Customer.objects.filter(auto_order=True, bi_weekly=True,
                                            permanent_cart_id__isnull=False)
    bi_uids = [] # biweekly_customers.values_list('user_id', flat=True)
    biweekly_customers_cache = {}
    for c in biweekly_customers:
        bi_uids.append(c.user_id)
        biweekly_customers_cache[c.user_id] = c
        # print(c.get_full_name())

    start_of_week = dt - timedelta(days=(dt.isoweekday() % 7) + 1)
    week_orders = Order.objects.filter(user_id__in=bi_uids,
            date__range=(start_of_week, start_of_week + timedelta(days=7))) \
        .exclude(status='canceled')

    for w in week_orders:
        c = biweekly_customers_cache[w.user_id]
        r = rc[str(w.route_id)]
        this_week_getters.append([c.get_full_name(), c.user.email,
                                  c.cell_phone, r.get_day_display(), r.title])

    last_week_orders = Order.objects.filter(user_id__in=bi_uids, date__lt=start_of_week,
                                    date__gte=start_of_week - timedelta(days=7)) \
                    .exclude(status='canceled')
    for w in last_week_orders:
        c = biweekly_customers_cache[w.user_id]
        r = rc[str(w.route_id)]
        last_week_getters.append([c.get_full_name(), c.user.email,
                                  c.cell_phone, r.get_day_display(), r.title])

    this_week_getters.append(['-------', '---------', '--------', '-------', '-------', '------'])
    this_week_getters.extend(last_week_getters)
    return render_as_csv(this_week_getters, ['שם','אימייל','טלפון','יום', 'קו חלוקה'], 'this_week.csv')
    # write_to_csv(last_week_getters, 'last_week.csv')
    
    # self.stdout.write('----------------------')


def permanents_report_1(request):
    res = []
    with_content = request.GET.get('c', None) == '1'
    with_ext = request.GET.get('e', None) == '1'
    perms = Cart.objects.filter(quantity__gt=0, is_perm=True) \
            .order_by('-id').select_related('user')
    if with_content:
        perms = perms.prefetch_related('lines')

    user_set = set()

    for p in perms:
        if p.user and p.user.is_active:
            if p.user_id in user_set:
                continue
            try:
                if p.user.profile.permanent_cart_id != p.id:
                    continue
            except Customer.DoesNotExist:
                continue
            last = p.user.profile.get_last_order()
            perm_cart_arr = []
            try:
                perm_cart_arr.extend([p.user.profile.get_full_name(), p.note])
            except:
                perm_cart_arr.extend([p.user.id, p.note])
            if with_content:
                content = ''
                for i in p.lines.all():
                    content += i.product.name + "\n"
                perm_cart_arr.append(content)
            perm_cart_arr.append('כן' if p.user.profile.bi_weekly else '')
            perm_cart_arr.append(last.date if last else '')
            user_set.add(p.user_id)
            res.append(perm_cart_arr)

    s_res = sorted(res, key=lambda x: x[0])
    filename = "permanents_notes.xls"
    book = Workbook(encoding='windows-1255')
    sheet = book.add_sheet('Sheet 1',cell_overwrite_ok=True)
    sheet.print_grid = True
    sheet.show_grid = True
    sheet.cols_right_to_left = True
    sheet.portrait = False
    # sheet.col(0).width = 36 * 256

    row_num = 0
    sheet.row(0).write(0, 'לקוח', strong_style)
    sheet.row(0).write(1, 'הערה', strong_style)
    i = 2
    if with_ext:
        sheet.row(0).write(i, 'הערה (פירות)', strong_style)
        i += 1
    if with_content:
        sheet.row(0).write(i, 'תכולה', strong_style)
        i += 1
    sheet.row(0).write(i, 'פעם בשבועיים', strong_style)
    i = 1
    for dat in s_res:
        sheet.row(i).write(0, dat[0], general_style)
        sheet.row(i).write(1, dat[1], general_style)
        col_idx = 2
        if with_ext:
            sheet.row(i).write(col_idx, dat[col_idx], general_style)
            col_idx += 1
        if with_content:
            sheet.row(i).write(col_idx, dat[col_idx], general_style)
            col_idx += 1
        sheet.row(i).write(col_idx, dat[col_idx], general_style)
        col_idx += 1
        sheet.row(i).write(col_idx, dat[col_idx], general_style)
        i += 1

    response = HttpResponse(content_type="application/vnd.openxmlformats-officedocument.spreadsheetml.sheet")
    response['Content-Disposition'] = 'attachment; filename=%s' % (filename,)
    book.save(response)
    return response

# def sapta_bill_exp(request):
    


def dictfetchall(cursor):
    "Return all rows from a cursor as a dict"
    columns = [col[0] for col in cursor.description]
    return [
        dict(list(zip(columns, row)))
        for row in cursor.fetchall()
    ]
    
    
def sql_for_all_tenants(request):
    logger = logging.getLogger(__name__)
    q_res = []
    ok_count = 0

    if not request.user.is_superuser or not request.user.email.startswith('am'):
        return HttpResponse('-- Not authorized --') 

    sql_file = open('/tmp/ezf_update.sql')
    if request.GET.get('q') == '1':
        sql_file = open('/tmp/ezf_query.sql')
    sql = "SELECT id, schema_name, settings, terminal_number, manager_id, user_name, ginvoice_private_key, type, \
              ginvoice_public_key FROM public.farm_farm"

    farms = []
    try:
        res = Farm.objects.raw(sql)
        for f in res:
            farms.append(f)

    except Exception as e:
        print(str(e))
        logger.warning(str(e))
        return HttpResponse('-------------------')

    cursor = connection.cursor()
    sql_base = ''  # 'set search_path to {0}; '.format(f.schema_name)
    for line in sql_file:
        if not line.startswith('#'):
            sql_base += line + ' '
    for f in farms:
        if f.schema_name != 'public':
            print(f.schema_name)
            sql = sql_base.replace('<sch>', f.schema_name)
            try:
                cursor.execute(sql)
        
            except Exception as e:
                logger.warning('executing {0} failed'.format(sql))
                cursor.close()
                cursor = connection.cursor()
                q_res.append(str(e))
            else:
                print('OK')
                try:
                    results = dictfetchall(cursor)
                    q_res.append(f.schema_name + ': ' + str(results))
                except:
                    q_res.append(f.schema_name + ': OK' )
                ok_count += 1

    cursor.close()
    return HttpResponse(str(ok_count) + '<br/>'.join(q_res) + '<br/> ------- Done --------')


def add_forgotten_notes(request):
    doit = request.GET.get('do', None)
    ret = []
    ordrs = get_current_orders()
    for o in ordrs:
        if o.user.profile.notes and not o.notes:
            if doit:
                o.notes = o.user.profile.notes
                o.save()
            ret.append(o.user.profile.get_full_name())

    return HttpResponse("<br/>".join(ret))


def abandoned_carts(request):
    from_date = timezone.now() - timedelta(days=5)
    carts = list(Cart.objects.filter(created__gt=from_date, quantity__gt=0, is_perm=False))
    c_ids = [c.id for c in carts]
    orders = Order.objects.filter(cart_id__in=c_ids)
    for ordr in orders:
        c_ids.remove(ordr.cart_id)
    ret = []
    for cart in carts:
        if cart.id in c_ids:
            if cart.user:
                ret.append(str(cart.id) + ' ' + cart.user.profile.get_full_name() \
                           + ' ' + str(cart.created.date()) + ' [' + str(cart.quantity) + ']')
    return HttpResponse('<br/>'.join(ret))


def system_msg(request):
    return render(request, 'extra/sys_msg.html', {})


def __dummy():
    replace_obj = ReplacementProduct(
        basket_product_id=443703,
        replaced_product_id=375,
        remove_only=True
    )
    return replace_obj

def make_rand_customer(uid, first, last):
    found = False
    while not found:
        route_id = random.randint(53, 81)
        if Route.objects.filter(pk=route_id).exists():
            found = True

    c = Customer(user_id=uid,
                first_name=first,
                last_name=last,
                city_id=random.randint(100,650),
                route_id=route_id,
                lang='HE',
                auto_order=False,
                bi_weekly=False,
                payment_method=3, 
                notes = ' ',
                note_from_manager=' ',
                balance_start_point=0,
                recurrence = ' ',   

                )
    return c



def find_unshipped(request):
    days_back = int(request.GET.get('back', '40'))
    ret = []
    to_date = date.today() - timedelta(days=10)
    from_date = date.today() - timedelta(days=days_back)
    ords = Order.objects.filter(status='accepted', date__lt=to_date, date__gt=from_date)

    if not ords.exists():
        return HttpResponse(' =========== None ==========')
    else:
        for o in ords:
            info = str(o.id) + ': ' + str(o.date) + ' '
            try:
                info += o.user.profile.get_full_name()
            except:
                pass
            ret.append(info)
        return HttpResponse('<br/>'.join(ret))

"""
from payment_processing.views import _create_op_10_tst
def _tst_token(request):
    g = request.GET.get('g')
    ok = '0'
    if int(g) == 1297:
        res = _create_op_10_tst(request)
        print(res)
        ok = '1'
    return HttpResponse("========== Done ======= " + ok )
"""

def freeze_orders(request=None):
    ents = LogEntry.objects.filter(change_message='orders_freeze')
    if ents.exists():
        ents.delete()
    if request.GET.get('reset') == '1':
        return HttpResponse('======== Done un-freeze ========')

    freeze_obj = LogEntry.objects.create(change_message='orders_freeze', user_id=request.user.id, action_flag=1, 
                                         object_repr='jj', action_time=datetime.now())
    if request:
        return HttpResponse('======== Done ========')


def recalculate_const_payment(request):
    changed_orders = 0
    const_p = get_const_payment_settings()
    if const_p:
        const_amount = const_p['amount']
        orders = get_current_orders(with_shipped=True)
        for o in orders:
            if o.const_payment != const_amount:
                diff = const_amount - o.const_payment
                o.const_payment = const_amount
                o.sum += diff
                o.taxable_sum += diff
                o.save()
                changed_orders += 1
    return HttpResponse("Done![%d orders updated]" % (changed_orders,))


def get_deleted_by_customer(request):
    txt = []
    link = '<a href="/manage/order/qedit/#/">order [date]</a><br/>'
    today = date.today()
    # orders = Order.objects.filter(last_edited__date=F('date'), status='canceled', registered_by__is_staff=False,
    #                                date__gte=today - timedelta(days=180))
    orders = Order.objects.filter(last_edited__date=F('date'), status='canceled', registered_by__is_staff=False,
                                    registration_date__gt=today - timedelta(days=180))
    for ordr in orders:
        if ordr.date == ordr.last_edited.date():
            this_link = link.replace('#', str(ordr.id))       
            txt.append(this_link.replace('date', str(ordr.date)))

    if not txt:
        return HttpResponse("=========== Not found ==========")
    else:
        return HttpResponse(''.join(txt))


def register_tenant(request):
    if request.method == 'POST':
        form = TenantForm(request.POST, request.FILES)
        if form.is_valid():
            schema_name = form.cleaned_data['schema']
            code = random.randint(10000, 99999)
            # print(code)
            res = ''
            cursor = connections['q&d'].cursor()
            sql = "insert into tenant (schema, title, phone, code, mail, subtitle) values("
            sql += "'{0}', '{1}', '{2}', {3}, '{4}', '{5}'".format(
                schema_name,
                form.cleaned_data['title'],
                form.cleaned_data['phone'],
                code,
                form.cleaned_data['from_address'],
                form.cleaned_data['subtitle'],
            ) 
            sql += ");"
            cursor.execute(sql)
            img_path = os.path.join(settings.MEDIA_ROOT, 'img', schema_name)
            data_path = os.path.join(settings.MEDIA_ROOT, 'data', schema_name)
            product_img_path = os.path.join(img_path, 'products')
            try:
                os.mkdir(img_path)
                os.mkdir(data_path)
                os.mkdir(product_img_path)
                if 'logo' in request.FILES:
                    f = request.FILES['logo']
                    saved_file_name = os.path.join(img_path, 'header.jpg')
                    destination = open(saved_file_name, 'wb+')
                    for chunk in f.chunks():
                        destination.write(chunk)
                    destination.close()
                else:
                    print('no logo')
            except:
                res += ' files creation failed'
            send_sms(['0506392027',], 'new registration to easyfarm. code: {0}, schema: {1}'\
                    .format(code, schema_name), 'easyFarm')
            title = 'new easyfarm registration '
            msg = 'new registration to easyfarm. code: {0}, schema: {1}'\
                    .format(code, schema_name)
            mail_superadmin(title, msg + ' ' + res)
            # print res
            return HttpResponse('============ Done ============ ' + res)

    else:
        form = TenantForm()
        return render(request, 'elements/generic_form.html',  {
                'form': form,
                'title': 'פרטי הרשמה למערכת easyfarm',
        })


def populate_initial_data(request, schema):
    stage = request.GET.get('stage', '')
    sql = ''

    if not stage or stage == '1':
        init_data_file = open(os.path.join(settings.MEDIA_ROOT, 'init', 'import1.sql'))
        for l in init_data_file.readlines():
            sql += l
        try:
            cursor = connections['raw'].cursor()
            cursor.execute('set search_path to {0}'.format(schema))
            cursor.execute(sql)
        except:
            pass
    
    if not stage or stage == '2':
        # create manager user in the target schema (this function is called fro the public schema)
        # first find the email of the tenant manager
        tenant = get_object_or_404(Farm, schema_name=schema)
        email = tenant.from_address
        pwd = 'giR8b{0}'.format(schema[-1:])
        try:
            temp = User.objects.create_user(email=email, password=pwd) 
            temp.save()
        except:
            print('creating temporary user (on the public schema) failed')
        username = email[:email.find('@')] + '_' + str(random.randint(11,100));
        sql = "insert into users_user(email, is_active, is_staff,is_superuser,date_joined,  password) \
                values('{0}', True, True, False,'{1}', '{2}')"
        cursor = connections['raw'].cursor()
        try:
            cursor.execute('set search_path to {0}'.format(schema))
            cursor.execute(sql.format(email, date.today(),temp.password))
        except:
            pass
    sql = ''

    if not stage or stage == '3':
        init_data_file = open(os.path.join(settings.MEDIA_ROOT, 'init', 'group_permissions.sql'))
        for l in init_data_file.readlines():
            sql += l.replace('<sch>', schema)
        cursor = connections['raw'].cursor()
        cursor.execute('set search_path to {0}'.format(schema))
        cursor.execute(sql)

    try:
        temp.email = temp.email[:5] + '_' + temp.email[5:]
        temp.is_staff = False
        temp.is_active = False
        temp.save()
    except:
        pass
    return HttpResponse('---- Done ----')


def create_tenant(request, code):
    cursor = connections['q&d'].cursor()
    domain = 'easyfarm.co.il'
    if request.GET.get('d', '') == 'webdev':
        domain = 'webdev.co.il'
    elif request.GET.get('d', '') == 'amir1':
        domain = 'amir1.online'
    sql = "select * from tenant where code={0} order by id DESC".format(code)
    cursor.execute(sql)
    res = dictfetchall(cursor)
    if not res:
        return HttpResponse('=== Failed, wrong code entered ===')
    schema = res[0]['schema']
    # if request.tenant.schema_name != 'public':
    #    return HttpResponse('=== FAIL === [you are not in the public schema]')
    if res:
        try:
            farm = Farm.objects.create(
                domain_url=schema + '.' + domain,
                schema_name=schema,
                name=res[0]['title'],
                site_title=res[0]['title'],
                subtitle=res[0]['subtitle'],
                manager_id=1,
                logo='',
                phone=res[0]['phone'],
                type=2,
                settings=' ',
                order_cycle=2,
                employees=5,
                from_address=res[0]['mail'],
                terminal_number=0,
                terminal_number_2=0
            )
        except Exception as e:
            print(str(e))
            return HttpResponse('farm creation failed ' + str(e))
        else:
            msg = 'Success!<br/>'
            msg += 'Now run this from shell: python manage.py migrate_schemas --run-syncdb -s <schema_name> <br/>'
            msg += 'Also, from db manager, do: <br/>'
            msg += "ALTER TABLE <sch>.pages_page ADD uuid uuid NULL;"
            return HttpResponse(msg)



def get_outer_div_range(txt, idx):
    start_idx = 0
    orig_idx = idx
    while txt[idx] != '<' or txt[idx:idx+4] != '<div':
        idx -= 1
    start_idx = idx
    # find end of outer div
    end_idx = txt.find('</div', orig_idx) + 6
    return (start_idx, end_idx)

def get_outer_footer_range(txt, idx):
    start_idx = 0
    orig_idx = idx
    while txt[idx] != '<' or txt[idx:idx+4] != '<foo':
        idx -= 1
    start_idx = idx
    idx = orig_idx + 10
    while txt[idx] != '<' or txt[idx:idx+5] != '</foo':
        idx += 1
    end_idx = idx + 9
    return (start_idx, end_idx)

# content types
RICH_TEXT = 1
SLIDER = 2
IMAGE = 3
FOOTER = 4
HEADER = 5
RAW_TEXT = 10
content_types_dict = dict([
    (RICH_TEXT, 'טקסט עשיר',),
    (SLIDER, 'סליידר מוצרים',),
    (IMAGE, 'תמונה',),
    (FOOTER, 'footer',),
    (HEADER, 'header',),
    (RAW_TEXT, 'טקסט פשוט',),
])

def _parse_tpl(tpl_text):
    ph_list = []
    matches = re.finditer(r'{% .*placeholder .* %}', tpl_text)
    m_cnt = 0
    for m in matches:
        if 'meta-tags' in m.group():
            continue
        m_cnt += 1
        ph = m.group()
        idx = m.start()
        end = m.end()
        mtch = re.search(r'placeholder [\'"]([\w \d-]+)[\'"]', ph)
        type = RAW_TEXT 
        if 'ckeditor' in ph:
            type = RICH_TEXT
        elif 'imageplaceholder' in ph:
            type = IMAGE
        elif 'slider' in ph:
            type = SLIDER
        if 'footer' in ph:
            type = FOOTER
        if mtch:
            name = mtch.group(1)
        else:
            name = 'title_{}'.format(m_cnt)
        if type == IMAGE:
            idx = tpl_text.rfind('{% block', idx - 200, idx)
            end = tpl_text.find('endblock', idx) + len('endblock %}')
            ph = tpl_text[idx:end]
        elif type != FOOTER:
            idx, end = get_outer_div_range(tpl_text, idx)
            ph = tpl_text[idx:end]
        else:
            ph = tpl_text[idx:end]
        ph_list.append({'name': name, 'start': idx, 'end': end, 'txt': ph, 'type': type})
    return ph_list


def del_block(request, page_id, name):
    try:
        b = Content.objects.get(page_id=page_id, type=name)
        b.delete()
    except Content.DoesNotExist:
        pass

    cur_page = get_object_or_404(Page, id=page_id)
    tpl = cur_page.template
    with open(pathlib.Path(settings.TEMPLATES[0]['DIRS'][0] / tpl), mode='r') as f:
        tpl_text = f.read()
        ph_list = _parse_tpl(tpl_text)
        
        for ph in ph_list:
            if ph['name'] == name:
                new_tpl = tpl_text[:ph['start']] + tpl_text[ph['end']:]
                with open(pathlib.Path(settings.TEMPLATES[0]['DIRS'][0] / tpl), mode='w') as f:
                    f.write(new_tpl)
                break

    messages.success(request, 'בלוק תוכן נמחק בהצלחה')
    return HttpResponseRedirect(reverse('extra:hp-composer'))


def change_block_order(request):
    new_ord = request.POST.get('order', None)
    if new_ord:
        cur_page = get_object_or_404(Page, id=1)
        tpl = cur_page.template
        with open(pathlib.Path(settings.TEMPLATES[0]['DIRS'][0] / tpl), mode='r') as f:
            tpl_text = f.read()
            # discard the footer (last element) from list
            ph_list = _parse_tpl(tpl_text)[:-1]
        # build a new placeholder list, ordered by the new order
        ordered_ph_list = []
        new_ord = new_ord.split(',')
        for key in new_ord:
            for ph in ph_list:
                if key == ph['name']:
                    ordered_ph_list.append(ph)
                    break
        for o in ordered_ph_list:
            print(o['name'])
        # rebuild file
        # html_chunks are html blocks located between the placeholder blocks
        html_chunks = []
        start_idx = 0
        for b in ph_list:
            html_chunks.append(tpl_text[start_idx:b['start']])
            start_idx = b['end']
        html_chunks.append(tpl_text[start_idx:])
        new_tpl = html_chunks[0]
        chunks_idx = 1
        for o in ordered_ph_list:
            new_tpl += o['txt']
            try:
                new_tpl += html_chunks[chunks_idx]
            except IndexError:
                pass
            chunks_idx += 1
        # new_tpl += html_chunks[chunks_idx]
        with open(pathlib.Path(settings.TEMPLATES[0]['DIRS'][0] / tpl), mode='w') as f:
            f.write(new_tpl)
            return HttpResponseRedirect(reverse('extra:hp-composer'))

        
        return JsonResponse({'status': 'ok'}, status=200)


def homepage_composer(request):
    p_holder_tpl = """
        <div class="plc_h row">
        <div class="p-3">
        {{% {0} %}}
        </div>
        </div>
    """
    footer_p_holder_tpl = '{{% {0} %}}'
    img_p_holder_tpl = """
        {% block '^' %}
            <div> 
                 {% imageplaceholder "^" block %}
                <a href="/"><img  class="img-responsive" src="{{ MEDIA_URL }}{{ content }}" alt="^"></a> 
                {% endplaceholder %}
            </div>
        {% endblock %}
    """
    cur_page = get_object_or_404(Page, id=1)
    tpl = cur_page.template
    with open(pathlib.Path(settings.TEMPLATES[0]['DIRS'][0] / tpl), mode='r') as f:
        tpl_text = f.read()
        ph_list = _parse_tpl(tpl_text)
    footer_exists = FOOTER in [x['type'] for x in ph_list]
    # print(ph_list)
    """
    for p in ph_list:
        print(p['name'])
        print('============================')
        print(p['txt'])
        print(' ')
    """
    if request.method == 'GET':
        return render(request, 'pages/homepage_composer.html', {
            'ph_list': ph_list,
            'footer_exists': footer_exists,
            'cur_page': cur_page,
            'content_types': content_types_dict,
        })
    elif request.method == 'POST':
        element_type = int(request.POST.get('elem_type'))
        ph_name = request.POST.get('ph_name').replace(' ', '-')
        # last_ph_name = request.POST.get('ph_name').replace(' ', '-')
        print(element_type, ph_name) 
        new_ph = ''
        if element_type == RICH_TEXT:
            inner = 'ckeditor_placeholder "{}" with ckeditor'.format(ph_name)
            new_ph = p_holder_tpl.format(inner)
        elif element_type == SLIDER:
            inner = 'slider_placeholder "{}"'.format(ph_name)
            new_ph = p_holder_tpl.format(inner)
        elif element_type == IMAGE:
            new_ph = img_p_holder_tpl.replace('^', ph_name)
        elif element_type == FOOTER:
            if FOOTER in [p['type'] for p in ph_list]:
                messages.error(request, 'הדף יכול לכלול רק אלמנט אחד מסוג footer')
                return HttpResponseRedirect(reverse('extra:hp-composer'))
            if not 'footer' in ph_name:
                ph_name += ' - footer'
            inner = 'ckeditor_placeholder "{}" with ckeditor'.format(ph_name)
            new_ph = footer_p_holder_tpl.format(inner)

        if element_type == FOOTER:
            # insert inside the '<footer>' tag
            idx = tpl_text.find('<footer')
            footer_start = tpl_text.find('>', idx) + 1
            footer_end = tpl_text.find('</footer>', footer_start)
            tpl_text = tpl_text[:footer_start] + '\n' + new_ph + '\n' + tpl_text[footer_end:]
        else:
            # insert after the last placeholder element, or if a footer placeholed 
            # already exists - before the footer
            if footer_exists:
                last_ph_end = ph_list[-2]['end']
            else:
                last_ph_end = ph_list[-1]['end']
            tpl_text = tpl_text[:last_ph_end] +  new_ph + tpl_text[last_ph_end:]

        with open(pathlib.Path(settings.TEMPLATES[0]['DIRS'][0] / tpl), mode='w') as f:
            f.write(tpl_text)
            return HttpResponseRedirect(reverse('extra:hp-composer'))


def fridge_products(request):
    ret = {}
    products = Product.objects.filter(is_fridge=True).values_list('id', 'name')
    for p in products:
        ret[p[0]] = p[1].replace('"', '')
    return JsonResponse(ret, status=200)


def update_coords(request, cid):
    customer = get_object_or_404(Customer, id=cid)
    try:
        coords = geocode_address(customer.addressline1, customer.city.name)
        lat_val = Decimal(coords[0]).quantize(Decimal('.00001'), rounding=ROUND_HALF_EVEN) 
        lng_val = Decimal(coords[1]).quantize(Decimal('.00001'), rounding=ROUND_HALF_EVEN)
        customer.set_prop_data('coord', {'lat': str(lat_val), 'lng': str(lng_val)})
        messages.success(request, 'Coordinates set')
        return HttpResponse('Coordinates set! [{0} , {1}]'.format(coords[0], coords[1]))
    except AddressNotFoundException:
        return HttpResponse('Failed to update customer coordinates - address is not legal')
    except AttributeError:
        return HttpResponse('Failed to update customer coordinates, address parts missing')

def get_contact_us_msg(request):
    if request.method == 'POST':
        msg = request.POST.get('message')
        print(msg)

    messages.success(request, 'ההודעה נשלחה בהצלחה')
    return HttpResponseRedirect('/pages/')


from product.product_util import *
from django.contrib.sites.shortcuts import get_current_site

def test(request):
    # from django.contrib.admin.models import LogEntry
    # ent = LogEntry(user_id=79, object_repr='---', action_flag=1, change_message='Suspend start')
    # ent.save()
    count = 0
    ccount = 0
    new_objects = []
    if request.session.exists('tkn'):
        print('pp')
    else:
        print('NF')

    s = get_current_site(request) 
    p_cache = get_price_list()

    r = get_object_or_404(Route, id=3)
    print(r.get_next_date())
    outp = ''
    j = 0
    for k,v in list(p_cache.items()):
        j += 1
        if j == 10:
            break
        outp += k + ': ' + ','.join([str(x) for x in list(v.values())]) + '<br/>'
    """
    for order in orders:
        if order.items.filter(id=2).exists():
            count += 1
            if order.sum > 6:
                order.discount_sum = 7
                order.sum -= 7
                order.save()
    """
    # mail_superadmin('my title', outp)
    return HttpResponse("========== Done =======<br/>" + s.name + ' ' + request.scheme + ' ' + s.domain + '<br/> ' + outp )


        
