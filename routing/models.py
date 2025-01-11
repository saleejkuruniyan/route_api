from django.db import models

class FuelStation(models.Model):
    stop_id = models.IntegerField()
    name = models.CharField(max_length=255)
    address = models.TextField()
    city = models.CharField(max_length=255)
    state = models.CharField(max_length=2)
    rack_id = models.IntegerField()
    latitude = models.FloatField()
    longitude = models.FloatField()
    price_per_gallon = models.FloatField()

    def __str__(self):
        return f"{self.name} - ${self.price_per_gallon}/gallon"
