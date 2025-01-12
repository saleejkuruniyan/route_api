import ast
import requests
from rest_framework.views import APIView
from rest_framework.response import Response
from rest_framework import status
from geopy.distance import geodesic
import googlemaps
from .models import FuelStation
from django.conf import settings
from scipy.spatial import cKDTree
import numpy as np
import logging

logger = logging.getLogger(__name__)

# Google Maps API Key
api_key = settings.GOOGLE_API_KEY
gmaps = googlemaps.Client(key=api_key)

class OptimalFuelRouteView(APIView):
    def post(self, request):
        try:
            start_location = ast.literal_eval(request.data["start_location"])  # Example: "(34.052235, -118.243683)"
            finish_location = ast.literal_eval(request.data["finish_location"])  # Example: "(36.778259, -119.417931)"
            truck_range = request.data.get("truck_range", 500)  # Optional truck range in miles
            fuel_efficiency = request.data.get("fuel_efficiency", 10)  # Optional fuel efficiency in mpg
            buffer_range = request.data.get("buffer_range", 50)  # Optional buffer range in miles
            deviation_limit = request.data.get("deviation_limit", 2)  # Optional deviation limit in miles

            if not start_location or not finish_location:
                return Response(
                    {"error": "start_location and finish_location are required fields."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Get polyline from Google Directions API
            logger.info("Fetching directions from Google Maps API...")
            directions = gmaps.directions(start_location, finish_location, mode="driving")

            if not directions or 'legs' not in directions[0]:
                return Response({"error": "No valid route found."}, status=status.HTTP_404_NOT_FOUND)

            polyline = directions[0]["overview_polyline"]["points"]
            decoded_path = googlemaps.convert.decode_polyline(polyline)[1:]
            logger.info(f"Decoded polyline into {len(decoded_path)} points.")

            total_distance = directions[0]["legs"][0]["distance"]["value"] / 1609.34  # Convert meters to miles
            logger.info(f"Total road distance from start to end: {total_distance} miles.")

            all_stations = FuelStation.objects.all()

            if not all_stations.exists():  # Check if QuerySet is empty
                return Response({'error': 'No fuel stations available'}, status=status.HTTP_400_BAD_REQUEST)

            # Check if the total distance is within the truck's range
            if total_distance <= truck_range:
                # Calculate the fuel cost using the average price of all stations
                average_price = sum(station.price_per_gallon for station in all_stations) / len(all_stations)
                logger.info(f"Average price per gallon: {average_price}")

                fuel_needed = total_distance / fuel_efficiency
                logger.info(f"Fuel needed: {fuel_needed} gallons")
                total_cost = fuel_needed * average_price
                logger.info(f"Total cost: {total_cost}")

                # Prepare a simplified response with a direct route map URL
                route_map_url = f"https://www.google.com/maps/dir/{start_location[0]},{start_location[1]}/{finish_location[0]},{finish_location[1]}"
                return Response({
                    "route_map_url": route_map_url,
                    "optimal_route": [],  # No intermediate stations needed
                    "total_cost": total_cost
                })

            # Find stations near the route
            station_coords = np.array([(station.latitude, station.longitude) for station in all_stations])
            station_tree = cKDTree(station_coords)

            # Query stations near each path point
            nearby_stations = {}
            path_coords = np.array([(point["lat"], point["lng"]) for point in decoded_path])

            # Convert deviation_limit from miles to degrees (~1 mile ≈ 0.0145 degrees)
            deviation_limit_degrees = deviation_limit / 69.0

            for path_point in path_coords:
                # Query stations within the deviation limit
                indices = station_tree.query_ball_point(path_point, deviation_limit_degrees)
                for idx in indices:
                    station = all_stations[idx]
                    station_id = station.stop_id
                    if station_id not in nearby_stations:
                        nearby_stations[station_id] = {
                            "id": station.stop_id,
                            "name": station.name,
                            "address": station.address,
                            "city": station.city,
                            "state": station.state,
                            "rack_id": station.rack_id,
                            "latitude": station.latitude,
                            "longitude": station.longitude,
                            "price_per_gallon": station.price_per_gallon,
                        }

            nearby_stations_list = list(nearby_stations.values())
            logger.info(f"Filtered to {len(nearby_stations_list)} stations within {deviation_limit} miles of the route.")

            # Sort nearby stations by distance from start_location
            nearby_stations_list.sort(
                key=lambda station: geodesic(
                    start_location, (station["latitude"], station["longitude"])
                ).miles
            )

            # Select optimal stations for refueling
            optimal_stations = []
            current_position = start_location

            while True:
                # Filter stations directly within the buffer range
                stations_in_buffer = [
                    station for station in nearby_stations_list
                    if (truck_range - buffer_range) <= geodesic(current_position, (station["latitude"], station["longitude"])).miles <= (truck_range - 25)]

                # If no stations are found, adjust buffer range and retry
                if not stations_in_buffer:
                    logger.info("No stations available within buffer range. Expanding buffer range.")
                    if buffer_range + 25 > truck_range:
                        return Response({"error": "No fuel stations found within the maximum distance truck can travel."}, status=status.HTTP_404_NOT_FOUND)
                    buffer_range += 25
                    continue

                logger.info(f"Found {len(stations_in_buffer)} stations within buffer range.")

                # Find the cheapest station
                cheapest_station = min(stations_in_buffer, key=lambda s: s["price_per_gallon"])
                logger.info(f"Selected Station: {cheapest_station}")

                # Add the station to the optimal stations list
                optimal_stations.append(cheapest_station)

                # Update the current position and remaining distance
                current_position = (cheapest_station["latitude"], cheapest_station["longitude"])
                remaining_distance = geodesic(current_position, finish_location).miles

                logger.info(f"Remaining Distance: {remaining_distance}")

                # Check if the destination is within range
                if remaining_distance <= truck_range:
                    logger.info("Destination is within range. Stopping further calculations.")
                    break

                # Remove visited stations from the list
                cheapest_station_index = nearby_stations_list.index(cheapest_station)
                nearby_stations_list = nearby_stations_list[cheapest_station_index + 1:]

                logger.info(f"Length of remaining nearby_stations_list: {len(nearby_stations_list)}")

            logger.info(f"No. of Optimal Stations: {len(optimal_stations)}")

            # Prepare locations for Google Distance Matrix API
            locations = [
                            f"{start_location[0]},{start_location[1]}"
                        ] + [
                            f"{station['latitude']},{station['longitude']}" for station in optimal_stations
                        ] + [
                            f"{finish_location[0]},{finish_location[1]}"
                        ]

            logger.info(f"Locations: {locations}")

            url = "https://maps.googleapis.com/maps/api/distancematrix/json"

            # Make a single Distance Matrix API call
            response = requests.get(url, params={
                "origins": "|".join(locations[:-1]),
                "destinations": "|".join(locations[1:]),
                "key": api_key
            }).json()

            # Extract distances in miles between adjacent locations
            distances = []
            for i in range(len(locations) - 1):
                element = response["rows"][i]["elements"][i]
                if element["status"] == "OK":
                    distances.append(element["distance"]["value"] / 1609.34)  # Convert meters to miles
                else:
                    distances.append(0)

            # Calculate fuel costs
            fuel_costs = []

            for i, distance in enumerate(distances):
                if i < len(optimal_stations):
                    price_per_gallon = optimal_stations[i]["price_per_gallon"]
                    logger.info(f"Price per gallon for this leg: {price_per_gallon}")
                else:
                    if optimal_stations:
                        price_per_gallon = sum([station['price_per_gallon'] for station in optimal_stations]) / len(optimal_stations)
                    else:
                        price_per_gallon = sum([station.price_per_gallon for station in all_stations]) / len(all_stations)
                    logger.info(f"Price per gallon for final leg: {price_per_gallon}")

                fuel_cost = (distance / fuel_efficiency) * price_per_gallon
                fuel_costs.append(fuel_cost)

            total_fuel_cost = sum(fuel_costs)

            # Output results
            logger.info(f"Distances (miles): {distances}")
            logger.info(f"Fuel Costs (USD): {fuel_costs}")
            logger.info(f"Total Fuel Cost (USD): {total_fuel_cost}")

            # Construct Google Maps URL with waypoints
            origin = locations[0]
            destination = locations[-1]
            waypoints = "|".join(locations[1:-1])
            route_map_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}&waypoints={waypoints}"

            # Return final response
            response_data = {
                "route_map_url": route_map_url,
                "optimal_route": optimal_stations,
                "total_cost": round(total_fuel_cost, 2),
            }
            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error: {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
