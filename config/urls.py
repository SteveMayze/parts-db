"""
URL configuration for config project.

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
from django.db import connection
from django.http import HttpResponse
from django.urls import include, path
from django.views.generic import RedirectView

def health(request):
    """Liveness check for the container health script: the app answers and the database opens."""
    with connection.cursor() as cursor:
        cursor.execute("SELECT 1")
    return HttpResponse("ok", content_type="text/plain")


urlpatterns = [
    path("health/", health, name="health"),
    path("", RedirectView.as_view(pattern_name="parts:list", permanent=False)),
    path('admin/', admin.site.urls),
    path("parts/", include("parts.urls")),
]
