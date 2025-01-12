import os, csv, time
import googlemaps
from concurrent.futures import ThreadPoolExecutor, as_completed
from django.core.management.base import BaseCommand
from django.db import transaction
from django.conf import settings
from routing.models import FuelStation

# Initialize Google Maps client
gmaps = googlemaps.Client(key=settings.GOOGLE_API_KEY)

def fetch_coordinates_google(address, retries=3, delay=2):
    """Fetch latitude and longitude for a given address using Google Maps API with retries."""
    attempt = 0
    while attempt < retries:
        try:
            result = gmaps.geocode(address)
            if result:
                location = result[0]['geometry']['location']
                print(f"{address}: {location['lat']}, {location['lng']}")
                return location['lat'], location['lng']
            else:
                print(f"Coordinates not found for {address}. Retrying...")
        except Exception as e:
            print(f"Error fetching coordinates for {address}: {e}. Retrying...")

        attempt += 1
        if attempt < retries:
            print(f"Retrying... ({attempt}/{retries})")
            time.sleep(delay)  # wait before retrying

    print(f"Failed to fetch coordinates for {address} after {retries} attempts.")
    return None, None


class Command(BaseCommand):
    help = "Geocode addresses from a CSV file and save to the FuelStation model."

    def add_arguments(self, parser):
        # Add the CSV file path as an argument
        parser.add_argument('file_path', type=str, help="Path to the CSV file with address data.")

    @transaction.atomic
    def handle(self, *args, **kwargs):
        file_path = kwargs['file_path']

        if not os.path.exists(file_path):
            self.stdout.write(self.style.ERROR(f"File not found: {file_path}"))
            return

        self.stdout.write(self.style.NOTICE(f"Processing CSV file: {file_path}"))

        # Read CSV and process records
        with open(file_path, 'r') as file:
            reader = csv.DictReader(file)
            addresses_to_geocode = []

            for row in reader:
                stop_id = row["OPIS Truckstop ID"]
                price_per_gallon = float(row['Retail Price'])
                station = FuelStation.objects.filter(stop_id=stop_id).first()

                if station:
                    # Update price_per_gallon if the record exists
                    station.price_per_gallon = price_per_gallon
                    station.save()
                    self.stdout.write(self.style.SUCCESS(f"Updated price for: {station.name}, {station.city}, {station.state} (Stop ID: {stop_id})"))
                else:
                    # Add to the list for geocoding
                    address = f"{row['Truckstop Name'].strip()}, {row['Address'].strip()}, {row['City'].strip()}, {row['State'].strip()}, USA"
                    addresses_to_geocode.append((row, address))

        # Use ThreadPoolExecutor for concurrent geocoding requests
        max_concurrent_requests = 50  # Google's rate limit for Geocoding API
        results = []

        with ThreadPoolExecutor(max_workers=max_concurrent_requests) as executor:
            future_to_data = {
                executor.submit(fetch_coordinates_google, address): row for row, address in addresses_to_geocode
            }

            for future in as_completed(future_to_data):
                try:
                    row = future_to_data[future]
                    latitude, longitude = future.result()
                    results.append((row, latitude, longitude))
                except Exception as e:
                    print(f"Error processing address: {row['Truckstop Name'].strip()}, {row['City'].strip()}, {row['State'].strip()}, Error: {e}")

        # Save new entries to the database
        for row, latitude, longitude in results:
            if latitude and longitude:
                FuelStation.objects.create(
                    stop_id=row["OPIS Truckstop ID"],
                    name=row['Truckstop Name'],
                    address=row['Address'],
                    city=row["City"],
                    state=row["State"],
                    rack_id=int(row["Rack ID"]),
                    latitude=latitude,
                    longitude=longitude,
                    price_per_gallon=float(row['Retail Price'])
                )
                self.stdout.write(self.style.SUCCESS(f"Added: {row['Truckstop Name'].strip()}, {row['City'].strip()}, {row['State'].strip()} at {latitude}, {longitude}"))
            else:
                self.stdout.write(self.style.WARNING(f"Skipping: {row['Truckstop Name'].strip()}, {row['City'].strip()}, {row['State'].strip()} , coordinates not found."))

        self.stdout.write(self.style.SUCCESS("Geocoding and updates completed successfully."))
