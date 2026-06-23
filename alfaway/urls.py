"""
URL configuration for alfaway project.

The `urlpatterns` list routes URLs to views. For more information please see:
    https://docs.djangoproject.com/en/5.2/topics/http/urls/
Examples:
Function views
    1. Add an import:  from my_app import views
    2. Add a URL to urlpatterns:  path('', views.home, name='home')
Class-based views
    1. Add an import:  from other_app.views import Home
    2. Add a URL to urlpatterns:  path('', Home.as_view(), name='home')
Including another URLconf
    1. Import the include() function: from django.urls import include, path
    2. Add a URL to urlpatterns:  path('blog/', include('blog.urls'))
"""
from django.contrib import admin
from django.http import JsonResponse
from django.urls import path
from django.views.generic import RedirectView

from api.views import (
    PresignedDownloadView,
    SFTPWebhookView,
    admission_detail,
    archive_received_file,
    business_expected_feed,
    business_expected_page,
    business_monitoring_feed,
    business_monitoring_page,
    download_received_file,
    enrol_feed,
    monitoring_causes,
    monitoring_feed,
    monitoring_page,
    recycle_file,
    reject_file,
    replay_admission,
    restore_received_file,
)

urlpatterns = [
    path('', RedirectView.as_view(url='/monitoring/', permanent=False), name='home'),
    path('admin/', admin.site.urls),
    path('api/internal/sftp-webhook/', SFTPWebhookView.as_view()),
    path('api/internal/files/<int:pk>/download-url/', PresignedDownloadView.as_view()),
    path('files/<int:pk>/download/', download_received_file, name='download-file'),
    path('files/<int:pk>/archive/', archive_received_file, name='archive-file'),
    path('files/<int:pk>/restore/', restore_received_file, name='restore-file'),
    path('monitoring/', monitoring_page, name='monitoring'),
    path('monitoring/feed/', monitoring_feed, name='monitoring-feed'),
    path('monitoring/causes/', monitoring_causes, name='monitoring-causes'),
    path('monitoring/business/', business_monitoring_page, name='monitoring-business'),
    path('monitoring/business/feed/', business_monitoring_feed,
         name='monitoring-business-feed'),
    path('monitoring/expected/', business_expected_page, name='monitoring-expected'),
    path('monitoring/expected/feed/', business_expected_feed,
         name='monitoring-expected-feed'),
    path('monitoring/admission/<int:pk>/', admission_detail, name='monitoring-admission'),
    path('monitoring/admission/<int:pk>/replay/', replay_admission, name='monitoring-admission-replay'),
    path('monitoring/feed/<int:pk>/enrol/', enrol_feed, name='monitoring-feed-enrol'),
    path('monitoring/files/<int:pk>/recycle/', recycle_file, name='monitoring-file-recycle'),
    path('monitoring/files/<int:pk>/reject/', reject_file, name='monitoring-file-reject'),
    path('healthz/', lambda r: JsonResponse({'status': 'ok'})),
]
