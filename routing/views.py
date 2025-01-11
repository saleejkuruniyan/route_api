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
            # Parse start and end locations
            start_location = ast.literal_eval(request.data["start_location"])  # Example: "(34.052235, -118.243683)"
            finish_location = ast.literal_eval(request.data["finish_location"])  # Example: "(36.778259, -119.417931)"

            truck_range = 500  # miles
            fuel_efficiency = 10  # mpg
            buffer_range = 25  # miles
            deviation_limit = 3  # miles

            if not start_location or not finish_location:
                return Response(
                    {"error": "start_location and finish_location are required fields."},
                    status=status.HTTP_400_BAD_REQUEST,
                )

            # Step 1: Get polyline from Google Directions API
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

            # Step 2: Find stations near the route
            station_coords = np.array([(station.latitude, station.longitude) for station in all_stations])
            station_tree = cKDTree(station_coords)

            # Step 2: Query stations near each path point
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
                    if station_id not in nearby_stations:  # Avoid duplicates
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

            # Convert the result to a list
            nearby_stations_list = list(nearby_stations.values())
            logger.info(f"Filtered to {len(nearby_stations_list)} stations within {deviation_limit} miles of the route.")

            # Sort nearby stations by distance from start_location
            nearby_stations_list.sort(
                key=lambda station: geodesic(
                    start_location, (station["latitude"], station["longitude"])
                ).miles
            )

            # Step 3: Select optimal stations for refueling
            # Initialize variables
            optimal_stations = []
            current_position = last_position = start_location
            remaining_distance = total_distance

            while remaining_distance > (truck_range - buffer_range):
                # Find all path points within the current buffer range
                candidate_points = []
                traveled_distance = 0
                last_point = current_position

                for path_point in decoded_path:
                    path_coords = (path_point["lat"], path_point["lng"])
                    # Calculate the incremental distance between consecutive points
                    distance_to_point = geodesic(last_point, path_coords).miles
                    traveled_distance += distance_to_point

                    # Check if this point is within the valid range
                    if (truck_range - buffer_range) <= traveled_distance <= (truck_range - 25):
                        candidate_points.append(path_coords)

                    # Stop if we've exceeded the truck's range
                    if traveled_distance > truck_range:
                        break

                    # Update last_point to the current path_coords
                    last_point = path_coords

                # If no candidate points are found, reduce buffer range and retry
                if not candidate_points:
                    logger.info("No valid points found within range. Expanding buffer range.")
                    buffer_range += 25
                    continue

                # Filter stations within buffer range of candidate points
                stations_in_buffer = []
                for station in nearby_stations_list:
                    station_coords = (station["latitude"], station["longitude"])
                    if any(geodesic(station_coords, point).miles <= buffer_range for point in candidate_points):
                        stations_in_buffer.append(station)

                # If no stations found, reduce buffer range and retry
                if not stations_in_buffer:
                    logger.info("No stations available within buffer range. Expanding buffer range.")
                    buffer_range += 25
                    continue
                logger.info(f"Found {len(stations_in_buffer)} stations within buffer range.")

                # Find the cheapest station
                cheapest_station = min(stations_in_buffer, key=lambda s: s["price_per_gallon"])

                # Add the station to the optimal stations list
                optimal_stations.append(cheapest_station)

                # Update the current position and remaining distance
                current_position = (cheapest_station["latitude"], cheapest_station["longitude"])
                remaining_distance -= geodesic(last_position, current_position).miles

                # Remove stations before the selected station
                nearby_stations_list = [
                    station for station in nearby_stations_list
                    if geodesic(current_position, (station["latitude"], station["longitude"])).miles > 0
                ]

                # Update start location for the next iteration
                last_position = current_position

            logger.info(f"No. of Optimal Stations: {len(optimal_stations)}")

            # Prepare locations for Google Distance Matrix API
            locations = [
                            f"{start_location[0]},{start_location[1]}"
                        ] + [
                            f"{station['latitude']},{station['longitude']}" for station in optimal_stations
                        ] + [
                            f"{finish_location[0]},{finish_location[1]}"
                        ]

            url = "https://maps.googleapis.com/maps/api/distancematrix/json"

            # Make a single API call
            response = requests.get(url, params={
                "origins": "|".join(locations[:-1]),
                "destinations": "|".join(locations[1:]),
                "key": api_key
            }).json()

            # Extract distances in miles
            distances = [
                element['distance']['value'] / 1609.34  # Convert meters to miles
                for row in response['rows']
                for element in row['elements']
            ]

            # Calculate fuel costs
            if optimal_stations:
                average_price = sum([station['price_per_gallon'] for station in optimal_stations]) / len(optimal_stations)
            else:
                average_price = sum([station.price_per_gallon for station in all_stations]) / len(all_stations)
            logger.info(f"Average price per gallon (USD): {round(average_price, 2)}")
            fuel_costs = [
                (distances[i] / fuel_efficiency) * optimal_stations[i]['price_per_gallon']
                for i in range(len(optimal_stations))
            ]
            # Add cost for the last leg
            fuel_costs.append((distances[-1] / fuel_efficiency) * average_price)
            total_cost = round(sum(fuel_costs), 2)

            logger.info(f"Fuel Costs per leg (USD): {fuel_costs}")
            logger.info(f"Total Cost (USD): {total_cost}")

            # Construct Google Maps URL with waypoints
            origin = locations[0]
            destination = locations[-1]
            waypoints = "|".join(locations[1:-1])
            route_map_url = f"https://www.google.com/maps/dir/?api=1&origin={origin}&destination={destination}&waypoints={waypoints}"

            # Return final response
            response_data = {
                "route_map_url": route_map_url,
                "optimal_route": optimal_stations,
                "total_cost": round(total_cost, 2),
            }
            return Response(response_data, status=status.HTTP_200_OK)

        except Exception as e:
            logger.error(f"Error: {str(e)}")
            return Response({"error": str(e)}, status=status.HTTP_500_INTERNAL_SERVER_ERROR)
