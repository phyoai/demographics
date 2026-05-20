import requests
from io import BytesIO
from PIL import Image, ExifTags, UnidentifiedImageError


def _ratio_to_float(value):
    if isinstance(value, tuple):
        return value[0] / value[1]
    if hasattr(value, "numerator") and hasattr(value, "denominator"):
        return value.numerator / value.denominator
    return float(value)


def _convert_to_degrees(value):
    degrees = _ratio_to_float(value[0])
    minutes = _ratio_to_float(value[1])
    seconds = _ratio_to_float(value[2])
    return degrees + (minutes / 60.0) + (seconds / 3600.0)


def _extract_exif(image):
    exif = image._getexif()
    if not exif:
        return {}

    parsed = {}
    for tag_id, val in exif.items():
        tag_name = ExifTags.TAGS.get(tag_id, tag_id)
        if tag_name == "GPSInfo":
            gps = {}
            for gps_tag_id, gps_val in val.items():
                gps_name = ExifTags.GPSTAGS.get(gps_tag_id, gps_tag_id)
                gps[gps_name] = gps_val
            parsed["GPSInfo"] = gps
        else:
            parsed[tag_name] = val
    return parsed


def find_geotags_from_image_url(image_url: str):
    try:
        response = requests.get(image_url, timeout=20)
        response.raise_for_status()

        image = Image.open(BytesIO(response.content))
        exif_data = _extract_exif(image)
        gps = exif_data.get("GPSInfo")

        if not gps:
            return {
                "success": False,
                "message": "No GPS EXIF data found in image.",
                "data": None
            }

        lat = gps.get("GPSLatitude")
        lat_ref = gps.get("GPSLatitudeRef")
        lon = gps.get("GPSLongitude")
        lon_ref = gps.get("GPSLongitudeRef")

        if not all([lat, lat_ref, lon, lon_ref]):
            return {
                "success": False,
                "message": "Incomplete GPS EXIF data found.",
                "data": None
            }

        latitude = _convert_to_degrees(lat)
        longitude = _convert_to_degrees(lon)

        if lat_ref != "N":
            latitude = -latitude
        if lon_ref != "E":
            longitude = -longitude

        return {
            "success": True,
            "message": "GPS data extracted successfully.",
            "data": {
                "latitude": latitude,
                "longitude": longitude
            }
        }

    except requests.RequestException as e:
        return {
            "success": False,
            "message": f"Failed to download image: {e}",
            "data": None
        }
    except UnidentifiedImageError:
        return {
            "success": False,
            "message": "URL did not return a valid image.",
            "data": None
        }
    except Exception as e:
        return {
            "success": False,
            "message": f"Unexpected error: {e}",
            "data": None
        }


# Example
result = find_geotags_from_image_url("https://scontent-ord5-1.cdninstagram.com/v/t51.71878-15/639985652_1331516678853008_8649141461938929532_n.jpg?stp=dst-jpg_e15_tt6&_nc_ht=scontent-ord5-1.cdninstagram.com&_nc_cat=108&_nc_oc=Q6cZ2gG9ESoTbY8piOapc7YZS6LyK22kKACBYHXFujr1qMzVmE4r93bJOlZ6xLPN_nPGfIM&_nc_ohc=Q08i6GT6KiAQ7kNvwHRGWXe&_nc_gid=UAP3BSrVUAZN7MTPf4Tpiw&edm=AOQ1c0wBAAAA&ccb=7-5&oh=00_Af0tBCB0fD-EniIWFdHbnq0fJtYFJ9Dc27fQBjfhZllEtg&oe=69E51AFB&_nc_sid=8b3546")
print(result)