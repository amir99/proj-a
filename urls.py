from django.urls import path, re_path
from django.views.generic import TemplateView

from extra import views

app_name = 'extra'

urlpatterns = [
   re_path(r'^baskets_report/$', views.baskets_report, name='basket-report'),
   re_path(r'^payments_report/(\d{1,2})/(\d{4,4})/$', views.payments_report),
   re_path(r'^discard_old_balance/$', views.discard_old_balance),
   re_path(r'^sort_products/$', views.sort_products),
   re_path(r'^perm_log/$', views.get_perm_log_entry),
   re_path(r'^find_duplicates/$', views.fix_duplicate_products),
   re_path(r'^check_orders_for_client/(\d{1,6})/$', views.check_orders_for_client),
   re_path(r'^set_no_mail/$', views.no_mail),
   re_path(r'^ano_misc/$', views.misc, name='misc'),
   re_path(r'^ano_create_customer/(\d{1,8})/$', views.create_customer),
   re_path(r'^close_todays_orders/$', views.close_todays_orders),
   re_path(r'^sql_all/$', views.sql_for_all_tenants),
   re_path(r'^get_set_farm_info/(\w{1})/(\w{1})/$', views.get_set_farm_info),
   re_path(r'^add_discount/$', views.add_discount_by_product),
   re_path(r'^add_forgotten_notes/$', views.add_forgotten_notes),
   re_path(r'^unshipped/$', views.find_unshipped),
   re_path(r'^show_system_msg/$', views.system_msg),
   re_path(r'^del_by_c/$', views.get_deleted_by_customer),
   re_path(r'^ano_register_tenant/$', views.register_tenant),
   re_path(r'^populate_initial/(\w{1,16})/$', views.populate_initial_data),
   re_path(r'^create_tenant/(\d{1,6})/$', views.create_tenant),
   re_path(r'^a_carts/$', views.abandoned_carts),
   path('ano_fridge_products', views.fridge_products, name='fridge-products'),
   path('homepage', views.homepage_composer, name='hp-composer'),
   path('del_block/<int:page_id>/<str:name>', views.del_block, name='del-block'),
   path('change_block_order', views.change_block_order, name='change-block-order'), 
   path('set_coords/<int:cid>/', views.update_coords, name='set-coords'),
   path('contact', views.get_contact_us_msg, name='contact-us'),
   # re_path(r'^bill-export/$', views.sapta_bill_exp),
   re_path(r'^test/$', views.test),
]
