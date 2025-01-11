from django.urls import path
from .views import OptimalFuelRouteView

urlpatterns = [
    path('get-route/', OptimalFuelRouteView.as_view(), name='get-route'),
]
