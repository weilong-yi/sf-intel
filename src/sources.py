"""Source system definitions: the three "vendor systems" we integrate.

Each entry says where the data lives (a DataSF / Socrata dataset), which
columns we pull, and which column is the event timestamp used for the
time-window filter. Keeping this in one place means adding a fourth
source is a config change, not a code change.
"""

SOCRATA_BASE = "https://data.sf.gov/resource"
WINDOW_DAYS = 90

SOURCES = {
    "police": {
        "dataset_id": "wg3w-h783",
        "label": "SFPD Incident Reports",
        "time_column": "incident_datetime",
        "columns": [
            "row_id", "incident_id", "incident_number", "cad_number",
            "incident_datetime", "report_datetime", "report_type_description",
            "incident_category", "incident_subcategory", "incident_description",
            "resolution", "intersection", "cnn", "police_district",
            "analysis_neighborhood", "latitude", "longitude",
        ],
    },
    "fire_ems": {
        "dataset_id": "nuek-vuh3",
        "label": "SFFD Fire/EMS Calls for Service",
        "time_column": "received_dttm",
        "columns": [
            "call_number", "unit_id", "incident_number", "call_type",
            "call_type_group", "received_dttm", "dispatch_dttm", "on_scene_dttm",
            "call_final_disposition", "address", "zipcode_of_incident",
            "station_area", "battalion", "priority", "final_priority",
            "unit_type", "neighborhoods_analysis_boundaries", "case_location",
        ],
    },
    "311": {
        "dataset_id": "vw6y-z8j6",
        "label": "SF 311 Cases",
        "time_column": "requested_datetime",
        "columns": [
            "service_request_id", "requested_datetime", "closed_date",
            "status_description", "agency_responsible", "service_name",
            "service_subtype", "service_details", "address", "street",
            "analysis_neighborhood", "lat", "long", "source",
        ],
    },
}
