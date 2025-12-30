from flask import Flask, render_template, request, redirect, url_for, session, make_response, send_from_directory, jsonify # Import the redirect function
from werkzeug.utils import secure_filename
import os
import ifcopenshell
import ifcopenshell.geom
import georeference_ifc
import re
import pyproj
from pyproj import Transformer
import pint
import numpy as np
import math
from scipy.optimize import leastsq
import pandas as pd
import json
from shapely.geometry import Polygon, mapping
import ifcopenshell.util.placement
import subprocess
import time
from flask_cors import CORS
# Load environment variables from .env file if it exists
try:
    from dotenv import load_dotenv
    load_dotenv()
except ImportError:
    # python-dotenv is not installed, skip loading .env file
    pass


app = Flask(__name__, static_url_path='/static', static_folder='static')
# Configure CORS to allow requests from localhost:3000 and other common development ports
CORS(app,
     origins=[
         'http://localhost:3000',
         'http://127.0.0.1:3000',
         'http://localhost:3001',
         'http://127.0.0.1:3001',
         'http://localhost:8080',
         'http://127.0.0.1:8080',
         'https://dev.geodatahub.ch',
         'https://www.geodatahub.ch'
     ],
     methods=['GET', 'POST', 'PUT', 'DELETE', 'OPTIONS'],
     allow_headers=['Content-Type', 'Authorization', 'X-Requested-With', 'Accept', 'Origin'],
     supports_credentials=True,
     expose_headers=['Content-Type', 'Access-Control-Allow-Origin'])
app.secret_key = '88746898'
app.config['UPLOAD_FOLDER'] = 'uploads'
ALLOWED_EXTENSIONS = {'ifc'}  # Define allowed file extensions as a set

# Environment configuration
API_BASE_URL = os.getenv('API_BASE_URL', 'http://localhost:8000')
# Global CORS handler for all responses
@app.after_request
def after_request(response):
    origin = request.headers.get('Origin')
    allowed_origins = [
        'http://localhost:3000',
        'http://127.0.0.1:3000',
        'http://localhost:3001',
        'http://127.0.0.1:3001',
        'https://dev.geodatahub.ch',
        'https://www.geodatahub.ch'
    ]
    if origin in allowed_origins:
        response.headers.add('Access-Control-Allow-Origin', origin)
        response.headers.add('Access-Control-Allow-Headers', 'Content-Type,Authorization,X-Requested-With,Accept,Origin')
        response.headers.add('Access-Control-Allow-Methods', 'GET,PUT,POST,DELETE,OPTIONS')
        response.headers.add('Access-Control-Allow-Credentials', 'true')
    return response
# Function to check if a filename has an allowed extension
def allowed_file(filename):
    return '.' in filename and filename.rsplit('.', 1)[1].lower() in ALLOWED_EXTENSIONS

def georef(ifc_file):
    geo = False
    #check ifc version
    version = ifc_file.schema
    message = f"IFC version: {version}\n"
    # Check the file is georefed or not
    mapconversion = None
    crs = None

    if ifc_file.schema[:4] == 'IFC4':
        project = ifc_file.by_type("IfcProject")[0]
        for c in (m for c in project.RepresentationContexts for m in c.HasCoordinateOperation):
            mapconversion = c
            crs = c.TargetCRS
        if mapconversion is not None:
            message += "IFC file is georeferenced.\n"
            geo = True
    if ifc_file.schema == 'IFC2X3':
        site = ifc_file.by_type("IfcSite")[0]
        psets = ifcopenshell.util.element.get_psets(site)
        if 'ePSet_MapConversion' in psets.keys() and 'ePSet_ProjectedCRS' in psets.keys():
            message += "IFC file is georeferenced.\n"
            geo = True
    return message , geo
        
def infoExt(filename , epsgCode):
    ureg = pint.UnitRegistry()
    ifc_file = fileOpener(filename)
    
    if ifc_file is None:
        errorMessage = "Could not open IFC file. Please check the file exists and is valid."
        return [], errorMessage
    
    #check ifc version
    version = ifc_file.schema
    messages = [('IFC version', version)]
    ifc_site = ifc_file.by_type("IfcSite")


    #Find Longtitude and Latitude
    RLat = ifc_site[0].RefLatitude
    RLon = ifc_site[0].RefLongitude
    RElev = ifc_site[0].RefElevation
    if RLat is not None and RLon is not None:
        x0= (float(RLat[0]) + float(RLat[1])/60 + float(RLat[2]+RLat[3]/1000000)/(60*60))
        y0= (float(RLon[0]) + float(RLon[1])/60 + float(RLon[2]+RLon[3]/1000000)/(60*60))
        session['Refl'] = True
        if hasattr(ifc_file.by_type("IfcSite")[0], "ObjectPlacement") and ifc_file.by_type("IfcSite")[0].ObjectPlacement.is_a("IfcLocalPlacement"):
            local_placement = ifc_file.by_type("IfcSite")[0].ObjectPlacement.RelativePlacement
                # Check if the local placement is an IfcAxis2Placement3D
            if local_placement.is_a("IfcAxis2Placement3D"):
                local_origin = local_placement.Location.Coordinates
                bx,by,bz= local_origin
                messages.append(('IFC Local Origin', local_origin))
            else:
                    errorMessage = "Local placement is not IfcAxis2Placement3D."
                    return messages, errorMessage
        else:
                errorMessage = "IfcSite does not have a local placement."
                return messages, errorMessage
    else:
        session['Refl'] = False
        messages.append(('RefLatitude or RefLongitude', 'Not available'))
    Refl = session.get('Refl')
    crs = None
    if ifc_file.schema[:4] != 'IFC4' and ifc_file.schema != 'IFC2X3':
        errorMessage = "IFC2X3, IFC4, and newer versions are supported.\n"
        return messages, errorMessage

    # Find local origin

                
    # Target CRS unit name
    try: 
        crs = pyproj.CRS.from_epsg(int(epsgCode))
    except:
        errorMessage = "CRS is not available."
        return messages, errorMessage


    crsunit = crs.axis_info[0].unit_name

    if crs.is_projected:
        messages.append(('Target CRS Type', 'Projected'))
        messages.append(('Target CRS EPSG', epsgCode))

    else:
        errorMessage = "CRS is not projected (geographic)."
        return messages, errorMessage
    target_epsg = "EPSG:"+str(epsgCode)
    transformer = Transformer.from_crs("EPSG:4326", target_epsg)
    # IFC length unit name
    ifc_units = ifc_file.by_type("IfcUnitAssignment")[0].Units
    for ifc_unit in ifc_units:
        if ifc_unit.is_a("IfcSIUnit") and ifc_unit.UnitType == "LENGTHUNIT":
            if ifc_unit.Prefix is not None:
                ifcunit = ifc_unit.Prefix + ifc_unit.Name
            else:
                ifcunit = ifc_unit.Name
    try: 
        quantity = unitmapper(ifcunit)
        ureg = pint.UnitRegistry()
        ifcmeter = quantity.to(ureg.meter).magnitude
    except:
        ifcmeter = None
    # try:
    #     if ifcunit in unit_mapping:
    #         quantity = 1 * unit_mapping[ifcunit]
    #         ifcmeter = quantity.to(ureg.meter).magnitude
    #     else:
    #         ifcmeter = None
    # except:
    #     ifcmeter = None
    try: 
        quantity = unitmapper(crsunit)
        ureg = pint.UnitRegistry()
        crsmeter = quantity.to(ureg.meter).magnitude
    except:
        crsmeter = None

    # try:
    #     if crsunit in unit_mapping:
    #         quantity = 1 * unit_mapping[crsunit]
    #         crsmeter = quantity.to(ureg.meter).magnitude
    #     else:
    #         crsmeter = None
    # except:
    #     crsmeter = None

    if crsmeter is not None and ifcmeter is not None:
        coeff= ifcmeter/crsmeter
        print(f"Unit conversion: IFC unit='{ifcunit}' ({ifcmeter}m), CRS unit='{crsunit}' ({crsmeter}m), coeff={coeff}")
    else:
        print(f"Unit conversion failed: IFC unit='{ifcunit}' (ifcmeter={ifcmeter}), CRS unit='{crsunit}' (crsmeter={crsmeter})")
        errorMessage = f"IFC/Map unit error: Could not determine unit conversion. IFC unit: {ifcunit}, CRS unit: {crsunit}"
        return messages, errorMessage
    if Refl:
        messages.append(("Reference Longitude",y0))
        messages.append(("Reference Latitude",x0))
        messages.append(("Reference Elevation",RElev))

    messages.append(("Target CRS Unit",str.lower(crsunit)))

    session['mapunit'] = str.lower(crsunit)

    if ifcunit:
        unit_name = ifcunit
        messages.append(("IFC Unit",str.lower(unit_name)))
        session['ifcunit'] = str.lower(unit_name)

    else:
        errorMessage = "No length unit found in the IFC file."
        return messages, errorMessage
    messages.append(("Unit Conversion Ratio",coeff))
    errorMessage = ""
    session['coeff'] = coeff
    if Refl:
        x1,y1,z1 = transformer.transform(x0,y0,RElev)
        x2= x1*coeff
        y2= y1*coeff
        session['xt'] = x1
        session['yt'] = y1
        session['zt'] = z1
        session['Longitude'] = y0
        session['Latitude'] = x0

    return messages, errorMessage

def unitmapper(value):
    ureg = pint.UnitRegistry()
    unit_mapping = {
    "METRE": ureg.meter,
    "METER": ureg.meter,
    "CENTIMETRE": ureg.centimeter,
    "CENTIMETER": ureg.centimeter,
    "MILLIMETRE": ureg.millimeter,
    "MILLIMETER": ureg.millimeter,
    "INCH": ureg.inch,
    "FOOT": ureg.foot,
    "YARD": ureg.yard,
    "MILE": ureg.mile,
    "NAUTICAL_MILE": ureg.nautical_mile,
    "metre": ureg.meter,
    "meter": ureg.meter,
    "centimeter": ureg.centimeter,
    "centimetre": ureg.centimeter,
    "millimeter": ureg.millimeter,
    "millimetre": ureg.millimeter,
    "inch": ureg.inch,
    "foot": ureg.foot,
    "yard": ureg.yard,
    "mile": ureg.mile,
    "nautical_mile": ureg.nautical_mile,
    # Add more mappings as needed
    }
    if value in unit_mapping:
            return  1 * unit_mapping[value]
    return

@app.route('/')
def index():
    return render_template('upload.html')

@app.route('/upload', methods=['POST'])
def upload_file():
    if 'file' not in request.files:
        return "No file part"
    file = request.files['file']
    if file.filename == '':
        return "No selected file"
    if file and allowed_file(file.filename):  # Check if the file extension is allowed
        filename = secure_filename(file.filename)
        file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
        ifc_file = fileOpener(filename)

        message, geo = georef(ifc_file)
        if geo:
            IfcMapConversion, IfcProjectedCRS = georeference_ifc.get_mapconversion_crs(ifc_file=ifc_file)
            df = pd.DataFrame(list(IfcProjectedCRS.__dict__.items()), columns= ['property', 'value'])
            dg = pd.DataFrame(list(IfcMapConversion.__dict__.items()), columns= ['property', 'value'])
            html_table_f = df.to_html()
            html_table_g = dg.to_html()
            IfcMapConversion, IfcProjectedCRS = georeference_ifc.get_mapconversion_crs(ifc_file=ifc_file)
            target = IfcProjectedCRS.Name.split(':')
            epsg = int(target[1])
            message2 = infoExt(filename,epsg)
            coeff = session.get('coeff')
            if coeff is None:
                return render_template('result.html', filename=filename, table_f=html_table_f, table_g=html_table_g, message=message2, api_base_url=API_BASE_URL)

            if int(coeff)!=1 and IfcMapConversion.Scale is None:
                message += "There is a conflict between Scale factor and unit conversion. (Yet to be decided by buildingSmart.)"
                session['scaleError']=True
                return render_template('result.html', filename=filename, table_f=html_table_f, table_g=html_table_g, message=message, api_base_url=API_BASE_URL)
            if int(coeff)!=1 and int(IfcMapConversion.Scale) == 1:
                message += "There is a conflict between Scale factor and unit conversion. (Yet to be decided by buildingSmart.)"
                session['scaleError']=True
            return render_template('result.html', filename=filename, table_f=html_table_f, table_g=html_table_g, message=message, api_base_url=API_BASE_URL)
        
        return redirect(url_for('convert_crs', filename=filename))  # Redirect to EPSG code input page
    else:
        return render_template('upload.html', error_message="Invalid file format. Please upload a .ifc file.")

@app.route('/devs', methods=['GET', 'POST'])
def devs_upload():
    if request.method == 'POST':
        if 'file' not in request.files:
            return "No file part"

        file = request.files['file']

        if file.filename == '':
            return "No selected file"

        if file and allowed_file(file.filename):
            filename = secure_filename(file.filename)
            file.save(os.path.join(app.config['UPLOAD_FOLDER'], filename))
            ifc_file = fileOpener(filename)

            # Check if the IFC file is georeferenced
            message, geo = georef(ifc_file)

            if geo:
                IfcMapConversion, IfcProjectedCRS = georeference_ifc.get_mapconversion_crs(ifc_file=ifc_file)
                dg = pd.DataFrame(list(IfcMapConversion.__dict__.items()), columns= ['property', 'value'])
                message += "IfcMapconversion:\n\n" + dg.to_string()
                return f"Filename: {filename}\nGeoreferenced: YES\n{message}"
            else:
                message += "For georeferencing the IFC file, please visit the following address in a web browser:\nhttps://ifcgref.bk.tudelft.nl"
                return f"Filename: {filename}\nGeoreferenced: NO\n{message}"
                
@app.route('/convert/<filename>', methods=['GET', 'POST'])
def convert_crs(filename):
    if request.method == 'POST':
        try:
            epsg_code = int(request.form['epsg_code'])
        except ValueError:
            message = "Invalid EPSG code. Please enter a valid integer."
            return render_template('convert.html', filename=filename, message=message)
        session['target_epsg'] = epsg_code
       # Call the infoExt function and unpack the results
        messages, error = infoExt(filename, epsg_code)
        if error == "":
            # Pass x2, y2, and z1 to the survey_points route
            return redirect(url_for('survey_points', filename=filename))
        return render_template('convert.html', filename=filename, message=error)

    return render_template('convert.html', filename=filename)

@app.route('/survey/<filename>', methods=['GET', 'POST'])
def survey_points(filename):
    epsg_code = session.get('target_epsg')
    
    # Check if we have auto points from interactive georeferencing
    auto_points = session.get('auto_points')
    if auto_points and auto_points.get('filename') == filename:
        # Auto-populate with interactive points
        session['target_epsg'] = auto_points.get('epsg_code', 'EPSG:2056')
        session['rows'] = len(auto_points.get('building_points', []))
        epsg_code = session.get('target_epsg')
        
        # Store the points for form population
        session['auto_building_points'] = auto_points.get('building_points', [])
        session['auto_map_points'] = auto_points.get('map_points', [])
        
        # Clear auto_points to avoid reprocessing
        session.pop('auto_points', None)
    
    messages, error = infoExt(filename, epsg_code)
    ifcunit = session.get('ifcunit')
    mapunit = session.get('mapunit')
    Refl = session.get('Refl')
    if request.method == 'POST':
        box_number = request.form.get('boxNumber')
        if box_number == '3':
            session['Refl'] = False
            Refl = False
    if Refl:
        messages , error = local_trans(filename,messages)
        Num = []
        if request.method == 'POST':
            try:
                Num = int(request.form['Num'])
                if Num < 0:
                    error += "Please enter zero or a positive integer."
                    return render_template('survey.html', filename=filename, messages=messages, error=error)
            except ValueError:
                error += "Please enter zero or a positive integer."
                return render_template('survey.html', filename=filename, messages=messages, error=error)
            session['rows'] = Num
            if Num == 0:
                return redirect(url_for('calculate', filename=filename))
        return render_template('survey.html', filename=filename, messages=messages, Num=Num, ifcunit=ifcunit, mapunit=mapunit, error=error, Refl = Refl)
    else:
        error += '\nThe model has no surveyed or georeferenced attribute.\nYou need to provide at least one point in local and target CRS.'
        error += '\n\nAccuracy of the results improves as you provide more georeferenced points.\nWithout any additional georeferenced points, it is assumed that the model is scaled based on unit conversion and rotation is derived from TrueNorth direction (if availalble).\n'
        Num = []
        if request.method == 'POST':
            try:
                Num = int(request.form['Num'])
                if Num <= 0:
                    error += "Please enter a positive integer."
                    return render_template('survey.html', filename=filename, error=error)
            except ValueError:
                error += "Please enter a positive integer."
                return render_template('survey.html', filename=filename, error=error)
            session['rows'] = Num
        return render_template('survey.html', filename=filename, messages=messages, Num=Num, ifcunit=ifcunit, mapunit=mapunit, Refl = Refl)


def local_trans(filename , messages):
    ifc_file = fileOpener(filename)
    xt = session.get('xt')
    yt = session.get('yt')
    zt = session.get('zt')
    bx,by,bz = 0,0,0
    error = ""
    
    try:
        sites = ifc_file.by_type("IfcSite")
        if not sites:
            error += "No IfcSite found in the model. "
            bx, by, bz = 0, 0, 0
        else:
            site = sites[0]
            if hasattr(site, "ObjectPlacement") and site.ObjectPlacement and site.ObjectPlacement.is_a("IfcLocalPlacement"):
                local_placement = site.ObjectPlacement.RelativePlacement
                # Check if the local placement is an IfcAxis2Placement3D
                if local_placement and local_placement.is_a("IfcAxis2Placement3D") and local_placement.Location:
                    local_origin = local_placement.Location.Coordinates
                    bx, by, bz = map(float, local_origin)
                    messages.append(("First Point Local Coordinates",str(local_origin)))
                else:
                    error += "Local placement is not IfcAxis2Placement3D or missing Location. "
                    bx, by, bz = 0, 0, 0
            else:
                error += "IfcSite does not have a local placement. "
                bx, by, bz = 0, 0, 0
    except Exception as e:
        error += f"Error reading site coordinates: {str(e)}. "
        bx, by, bz = 0, 0, 0
    
    session['bx'] = bx
    session['by'] = by        
    session['bz'] = bz        

    messages.append(("First Point Target coordinates" , ("(" + f"{xt:.6f}" + ", " + f"{yt:.6f}" + ", " + f"{zt:.6f}" + ")")))
    error += '\n\nAccuracy of the results improves as you provide more georeferenced points.\nWithout any additional georeferenced points, it is assumed that the model is scaled based on unit conversion and rotation is derived from TrueNorth direction (if available).\n'

    ifc_file = ifc_file.end_transaction()
    return messages, error

@app.route('/calc/<filename>', methods=['GET', 'POST'])
def calculate(filename):
    #if request.method == 'POST':
        # Access the form data by iterating through the rows
        coeff = session.get('coeff')
        rows = session.get('rows')
        fn = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        ifc_file = fileOpener(filename)
        
        if ifc_file is None:
            return "Error: Could not open IFC file. Please check the file exists and is valid.", 400
        
        # Validate required session variables
        if coeff is None:
            # Try to recalculate coefficient as fallback
            try:
                # Get IFC unit
                ifc_units = ifc_file.by_type("IfcUnitAssignment")[0].Units
                ifcunit = None
                for ifc_unit in ifc_units:
                    if ifc_unit.is_a("IfcSIUnit") and ifc_unit.UnitType == "LENGTHUNIT":
                        if ifc_unit.Prefix is not None:
                            ifcunit = ifc_unit.Prefix + ifc_unit.Name
                        else:
                            ifcunit = ifc_unit.Name
                        break
                
                # Get target CRS unit (assume meters if not available)
                crsunit = session.get('mapunit', 'meter')
                
                if ifcunit:
                    # Calculate coefficient
                    try:
                        ifc_quantity = unitmapper(ifcunit)
                        crs_quantity = unitmapper(crsunit)
                        ureg = pint.UnitRegistry()
                        ifcmeter = ifc_quantity.to(ureg.meter).magnitude
                        crsmeter = crs_quantity.to(ureg.meter).magnitude
                        coeff = ifcmeter / crsmeter
                        session['coeff'] = coeff
                        print(f"Recalculated coefficient: {coeff} (IFC: {ifcunit}={ifcmeter}m, CRS: {crsunit}={crsmeter}m)")
                    except:
                        coeff = 0.001  # Default assumption: IFC in mm, CRS in meters
                        session['coeff'] = coeff
                        print(f"Using default coefficient: {coeff}")
                else:
                    coeff = 0.001  # Default assumption: IFC in mm, CRS in meters
                    session['coeff'] = coeff
                    print(f"Using default coefficient (no IFC unit found): {coeff}")
                    
            except Exception as e:
                print(f"Failed to recalculate coefficient: {e}")
                return "Error: Missing scaling coefficient and unable to recalculate. Please restart the georeferencing process.", 400
        
        if rows is None:
            return "Error: Missing row count information. Please restart the georeferencing process.", 400
        
        data_points = []
        Refl = session.get('Refl')
        if Refl:
            xt = session.get('xt')
            yt = session.get('yt')
            zt = session.get('zt')
            bx = session.get('bx')
            by = session.get('by')
            bz = session.get('bz')
            
            # Check if reflection coordinates are available
            missing_vars = []
            if xt is None: missing_vars.append('xt')
            if yt is None: missing_vars.append('yt')
            if zt is None: missing_vars.append('zt')
            if bx is None: missing_vars.append('bx')
            if by is None: missing_vars.append('by')
            if bz is None: missing_vars.append('bz')
            
            if missing_vars:
                # If reflection coordinates are missing, fall back to non-reflection mode
                print(f"Warning: Missing reflection coordinates: {', '.join(missing_vars)}. Falling back to manual coordinate input mode.")
                Refl = False
                session['Refl'] = False
            else:
                data_points.append({"X": bx, "Y": by, "Z": bz, "X_prime": xt, "Y_prime": yt, "Z_prime":zt})
        #seperater
        if not Refl and rows == 1:
            Rotation_solution = 0
            # Use correct coefficient for proper IFC transformation
            if coeff == 0:
                return "Error: Invalid scaling coefficient (cannot be zero). Please restart the georeferencing process.", 400
                
            S_solution = coeff  # This correctly scales from IFC units to CRS units
            ro = ifc_file.by_type("IfcGeometricRepresentationContext")[0].TrueNorth
            if ro is not None and ro.is_a("IfcDirection"):
                xord , xabs = round(float(ro[0][0]),6) , round(float(ro[0][1]),6)
            else:
                xord , xabs = 0 , 1
            Rotation_solution = math.atan2(xord,xabs)
            A = math.cos(Rotation_solution)
            B = math.sin(Rotation_solution)
            # Fix coordinate transformation - use correct transformation formula  
            # Standard 2D transformation: [X'] = [cos -sin] [X*S] + [E]
            #                             [Y']   [sin  cos] [Y*S]   [N]
            E_solution = float(request.form[f'x_prime{0}']) - (A*float(request.form[f'x{0}'])*S_solution - B*float(request.form[f'y{0}'])*S_solution)
            N_solution = float(request.form[f'y_prime{0}']) - (B*float(request.form[f'x{0}'])*S_solution + A*float(request.form[f'y{0}'])*S_solution)
            session['zt'] = float(request.form[f'z_prime{0}'])
            session['bz'] = float(request.form[f'z{0}'])
            H_solution = float(request.form[f'z_prime{0}']) - (float(request.form[f'z{0}'])*S_solution)

        #seperater
        else:
            if rows == 0:
                Rotation_solution = 0
                # Use correct coefficient for proper IFC transformation
                if coeff == 0:
                    return "Error: Invalid scaling coefficient (cannot be zero). Please restart the georeferencing process.", 400
                
                S_solution = coeff  # This correctly scales from IFC units to CRS units
                ro = ifc_file.by_type("IfcGeometricRepresentationContext")[0].TrueNorth
                if ro is not None and ro.is_a("IfcDirection"):
                    xord , xabs = round(float(ro[0][0]),6) , round(float(ro[0][1]),6)
                else:
                    xord , xabs = 0 , 1
                Rotation_solution = math.atan2(xord,xabs)
                A = math.cos(Rotation_solution)
                B = math.sin(Rotation_solution)
                
                # Get session variables with validation
                xt = session.get('xt')
                yt = session.get('yt')
                zt = session.get('zt')
                bx = session.get('bx')
                by = session.get('by')
                bz = session.get('bz')
                
                if any(var is None for var in [xt, yt, zt, bx, by, bz]):
                    return "Error: Missing coordinate data for georeferencing. Please restart the georeferencing process.", 400
                
                # Fix coordinate transformation - use correct transformation formula
                # Standard 2D transformation: [X'] = [cos -sin] [X*S] + [E]
                #                             [Y']   [sin  cos] [Y*S]   [N]
                E_solution = xt - (A*S_solution*bx - B*S_solution*by)
                N_solution = yt - (B*S_solution*bx + A*S_solution*by)
                H_solution = zt - (S_solution*bz)
            else:
                for row in range(rows):
                    x = request.form[f'x{row}']
                    y = request.form[f'y{row}']
                    z = request.form[f'z{row}']
                    x_prime = request.form[f'x_prime{row}']
                    y_prime = request.form[f'y_prime{row}']
                    z_prime = request.form[f'z_prime{row}']

                    try:
                        x = float(x)
                        y = float(y)
                        z = float(z)
                        x_prime = float(x_prime)
                        y_prime = float(y_prime)
                        z_prime = float(z_prime)
                    except ValueError:
                        message = "Invalid input. Please enter only float values."
                        Num = rows
                        return render_template('survey.html', message=message, Num=Num)

                    data_points.append({"X": x, "Y": y, "Z":z,"X_prime": x_prime, "Y_prime": y_prime, "Z_prime": z_prime})

                def equations(variables, data_points):
                        S, Rotation, E, N , H = variables
                        eqs = []

                        for data in data_points:
                            X = data["X"]
                            Y = data["Y"]
                            Z = data["Z"]
                            X_prime = data["X_prime"]
                            Y_prime = data["Y_prime"]
                            Z_prime = data["Z_prime"]

                            # Standard 2D transformation equations
                            # X' = S * (cos*X - sin*Y) + E
                            # Y' = S * (sin*X + cos*Y) + N
                            eq1 = S * (np.cos(Rotation) * X - np.sin(Rotation) * Y) + E - X_prime
                            eq2 = S * (np.sin(Rotation) * X + np.cos(Rotation) * Y) + N - Y_prime
                            eq3 = S*Z + H - Z_prime
                            eqs.extend([eq1, eq2, eq3])

                        return eqs
                    # Initial guess for variables [S, Rotation, E, N]
                if Refl:
                    initial_guess = [coeff, 0, xt, yt, zt]
                else:
                    try:
                        x0 = float(request.form[f'x{0}'])
                        y0 = float(request.form[f'y{0}'])
                        z0 = float(request.form[f'z{0}'])
                        x_prime0 = float(request.form[f'x_prime{0}'])
                        y_prime0 = float(request.form[f'y_prime{0}'])
                        z_prime0 = float(request.form[f'z_prime{0}'])
                        
                        xg = x_prime0 - (x0 * coeff)
                        yg = y_prime0 - (y0 * coeff)
                        zg = z_prime0 - (z0 * coeff)
                        initial_guess = [coeff, 0, xg, yg, zg]
                    except (ValueError, KeyError) as e:
                        return f"Error: Invalid form data for initial coordinates. {str(e)}", 400

                # Perform the least squares optimization for all data points
                result = leastsq(equations, initial_guess, args=(data_points,), full_output=True)
                S_solution, Rotation_solution, E_solution, N_solution, H_solution = result[0]

        Rotation_degrees = (180 / math.pi) * Rotation_solution
        rDeg = Rotation_degrees - (360*round(Rotation_degrees/360))

        # Debug output for coordinate transformation validation
        print(f"Coordinate Transformation Results:")
        print(f"  Scale (S_solution): {S_solution}")
        print(f"  Rotation (degrees): {rDeg:.6f}")
        print(f"  Translation E: {E_solution:.6f}")
        print(f"  Translation N: {N_solution:.6f}")
        print(f"  Translation H: {H_solution:.6f}")
        print(f"  Unit coefficient: {coeff}")
        print(f"  X-axis abscissa: {math.cos(Rotation_solution):.6f}")
        print(f"  X-axis ordinate: {math.sin(Rotation_solution):.6f}")

        target_epsg = "EPSG:"+str(session.get('target_epsg'))
        georeference_ifc.set_mapconversion_crs(ifc_file=ifc_file,
                                        target_crs_epsg_code=target_epsg,
                                        eastings=E_solution,
                                        northings=N_solution,
                                        orthogonal_height=H_solution,
                                        x_axis_abscissa=math.cos(Rotation_solution),
                                        x_axis_ordinate=math.sin(Rotation_solution),
                                        scale=S_solution)
        fn_output = re.sub(r'\.ifc$','_georeferenced.ifc', fn)
        ifc_file.write(fn_output)
        IfcMapConversion, IfcProjectedCRS = georeference_ifc.get_mapconversion_crs(ifc_file=ifc_file)
        df = pd.DataFrame(list(IfcProjectedCRS.__dict__.items()), columns= ['property', 'value'])
        dg = pd.DataFrame(list(IfcMapConversion.__dict__.items()), columns= ['property', 'value'])
        dg['value'] = dg['value'].astype(str)
        html_table_f = df.to_html()
        html_table_g = dg.to_html()
        return render_template('result.html', filename=filename, table_f=html_table_f, table_g=html_table_g, api_base_url=API_BASE_URL)
    
def fileOpener(filename):
    fn = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    print("Opening IFC file:", fn)  # Add this line for debugging
    try:
        if not os.path.exists(fn):
            print(f"File does not exist: {fn}")
            return None
        ifc_file = ifcopenshell.open(fn)
        if ifc_file is None:
            print("Failed to open IFC file")
            return None
        return ifc_file
    except Exception as e:
        print("Error opening IFC file:", str(e))  # Add this line for debugging
        return None

@app.route('/show/<filename>', methods=['POST'])
def visualize(filename):
    fn = os.path.join(app.config['UPLOAD_FOLDER'], filename)
    fn_output = re.sub(r'\.ifc$','_georeferenced.ifc', fn)
    if not os.path.exists(fn_output):
        fn_output = fn
    ifc_file = ifcopenshell.open(fn_output)
    IfcMapConversion, IfcProjectedCRS = georeference_ifc.get_mapconversion_crs(ifc_file=ifc_file)
    target = IfcProjectedCRS.Name.split(':')
    org = ifc_file.by_type('IfcProject')[0].RepresentationContexts[0].WorldCoordinateSystem.Location.Coordinates
    E = IfcMapConversion.Eastings
    N = IfcMapConversion.Northings
    S = IfcMapConversion.Scale
    if S is None:
        S = 1
    ortz = IfcMapConversion.OrthogonalHeight
    cos = IfcMapConversion.XAxisAbscissa
    if cos is None:
        cos = 1    
    sin = IfcMapConversion.XAxisOrdinate
    if sin is None:
        sin = 0
    Rotation_solution = math.atan2(sin,cos)
    A = math.cos(Rotation_solution)
    B = math.sin(Rotation_solution)        
    target_epsg = "EPSG:"+ target[1]
    transformer2 = Transformer.from_crs(target_epsg,"EPSG:4326", always_xy=True)
    scaleError = session.get('scaleError')
    Gx , Gy = 0 , 0
    eff = session.get('coeff')
    if scaleError:
        saver = S
        S = eff
        session.pop('scaleError', None)  # Corrected line
        E=E*S
        N = N*S
        ortz= ortz*S
        # Apply correct transformation: [X'] = [cos -sin] [X*S] + [E]
        #                               [Y']   [sin  cos] [Y*S]   [N]
        xx = S * org[0] * A - S * org[1] * B + E
        yy = S * org[0] * B + S * org[1] * A + N
        z = S * org[2] + ortz
        S = saver
        Snew = S
    else:
        # Apply correct transformation: [X'] = [cos -sin] [X*S] + [E]
        #                               [Y']   [sin  cos] [Y*S]   [N]
        xx = S * org[0] * A - S * org[1] * B + E
        yy = S * org[0] * B + S * org[1] * A + N
        zz = S * org[2] + ortz
        Snew = S/eff
    if xx==0 and yy==0:
        products = ifc_file.by_type('IfcProduct')
        for product in products:
            if product.Representation:
                placement = product.ObjectPlacement
                lpMAat = ifcopenshell.util.placement.get_local_placement(placement)
                Gx , Gy = lpMAat[0][3]*eff,lpMAat[1][3]*eff
                xx = xx+Gx
                yy = yy+Gy
                break
    # Transform from target EPSG to WGS84 (lat/lng)
    # with always_xy=True, transformer returns (longitude, latitude)
    lng, lat = transformer2.transform(xx, yy)
    Latitude = lat
    Longitude = lng
    projstring = pyproj.CRS(target_epsg).to_proj4()
    crs = pyproj.CRS(projstring)
    alpha_value = crs.to_dict().get('alpha', None)
    Scale_value = crs.to_dict().get('k', None)
    if Scale_value is None:
        Scale_value = 1
    # if Scale_value is not None:
    #     Snew = Snew/Scale_value
    # if alpha_value is not None:
    transformer3 = Transformer.from_crs(target_epsg, "EPSG:3857", always_xy=True)
    x_3857, y_3857 = transformer3.transform(xx, yy)
    xn_3857, yn_3857 = transformer3.transform(xx+1000, yy)
    dx = -x_3857 + xn_3857
    dy = -y_3857 + yn_3857
    angle_radians = math.atan2(dy, dx)
    angle_degrees = math.degrees(angle_radians)
    Rotation_solution = Rotation_solution + angle_radians
    length = math.sqrt(dx**2 + dy**2)
    # transformer3 = Transformer.from_crs(target_epsg, "EPSG:3857", always_xy=True)
    x_3857, y_3857 = transformer3.transform(xx, yy)
    xn_3857, yn_3857 = transformer3.transform((xx+50), (yy+50))
    dx = -x_3857 + xn_3857
    dy = -y_3857 + yn_3857
    length = math.sqrt(dx**2 + dy**2)
    min_z_value = float('inf')

    for product in ifc_file.by_type('IfcProduct'):
    # Check if the product has a placement
        if product.Representation:
            placement = product.ObjectPlacement
            location = ifcopenshell.util.placement.get_local_placement(placement)
            z_value = location[2][3]*eff  # Z is usually the 3rd coordinate (0: X, 1: Y, 2: Z)            
            if z_value < min_z_value:
                min_z_value = z_value
    return render_template('view3D.html', filename=filename, Latitude=Latitude, Longitude=Longitude, Rotate=Rotation_solution, origin = org, Scale = Snew, ScaleCRS = Scale_value, Gx=Gx, Gy=Gy, LowestLevel = min_z_value)

@app.route('/download/<filename>', methods=['GET'])
def download(filename):
    # Define the path to the GeoJSON file
    fn = re.sub(r'\.ifc$','_georeferenced.ifc', filename)
    file_path = os.path.join(app.config['UPLOAD_FOLDER'], fn)

    # Ensure the file exists
    if os.path.exists(file_path):
        # Set the response headers to indicate a file download
        response = make_response()
        response.headers['Content-Type'] = 'application/octet-stream'
        response.headers['Content-Disposition'] = f'attachment; filename={fn}'
        
        # Read the file content and add it to the response
        with open(file_path, 'rb') as file:
            response.data = file.read()
        
        return response
    else:
        # Return a 404 error if the file doesn't exist
        return 'File not found', 404
@app.route('/templates/<path:filename>')
def temp(filename):
    return send_from_directory('templates', filename)
   
@app.route('/uploads/<path:filename>')
def ups(filename):
    return send_from_directory('uploads', filename)

@app.route('/chek',  methods=['GET', 'POST'])
def chs():
    return render_template('chek.html')

@app.route('/interactive')
def interactive_georef():
    """Interactive georeferencing tool"""
    return render_template('interactive_georef.html')

@app.route('/auto_georeference', methods=['POST'])
def auto_georeference():
    """Auto georeference endpoint that directly processes and redirects to result"""
    try:
        data = request.json
        filename = data.get('filename')
        building_points = data.get('buildingPoints', [])
        map_points = data.get('mapPoints', [])
        epsg_code = data.get('epsgCode', 'EPSG:2056')  # Default to EPSG:2056 for Switzerland
        
        # Store everything in session
        session['target_epsg'] = int(epsg_code.replace('EPSG:', ''))
        session['rows'] = len(building_points)
        session['auto_building_points'] = building_points
        session['auto_map_points'] = map_points
        session['filename'] = filename
        session['auto_mode'] = True
        
        # Also store coordinate data for direct processing
        for i, (building_point, map_point) in enumerate(zip(building_points, map_points)):
            session[f'x{i}'] = building_point['building']['x']
            session[f'y{i}'] = building_point['building']['y'] 
            session[f'z{i}'] = building_point['building']['z']
            session[f'x_prime{i}'] = map_point['coordinates']['easting']
            session[f'y_prime{i}'] = map_point['coordinates']['northing']
            session[f'z_prime{i}'] = map_point['coordinates']['elevation']
        
        return jsonify({
            'success': True,
            'redirect_url': f'/direct_georeference/{filename}'
        })
    except Exception as e:
        return jsonify({'success': False, 'error': str(e)})

@app.route('/direct_georeference/<filename>')
def direct_georeference(filename):
    """Direct route that skips all forms and goes straight to processing"""
    try:
        # Check if we have auto data
        if not session.get('auto_mode'):
            return redirect(url_for('upload'))
        
        # Get data from session
        building_points = session.get('auto_building_points', [])
        map_points = session.get('auto_map_points', [])
        rows = session.get('rows', 0)
        
        if rows == 0:
            return "No points to process", 400
            
        # Render the coordinate input form pre-filled
        return render_template('survey.html', 
                             filename=filename, 
                             Num=rows,
                             auto_mode=True,
                             building_points=building_points,
                             map_points=map_points,
                             messages=[('Auto Georeferencing', f'Processing {rows} points automatically')],
                             ifcunit='MILLIMETRE',
                             mapunit='METRE',
                             Refl=False)
    except Exception as e:
        return f"Error: {str(e)}", 500

@app.route('/test-auto')
def test_auto_georef():
    """Test page for automatic georeferencing workflow"""
    return render_template('test_auto_georef.html')

@app.route('/simple-test')
def simple_test():
    """Simple test page for auto-georeferencing"""
    return render_template('simple_test.html')
@app.route('/api/analyze-georeferencing', methods=['POST', 'OPTIONS'])
def analyze_georeferencing():
    """
    Comprehensive API endpoint to analyze IFC file georeferencing information
    Accepts file upload and returns detailed analysis including coordinates, georeferencing status, and spatial data
    """
    # Handle preflight OPTIONS request
    if request.method == 'OPTIONS':
        response = make_response()
        response.headers.add("Access-Control-Allow-Origin", "http://localhost:3000")
        response.headers.add("Access-Control-Allow-Headers", "Content-Type,Authorization,X-Requested-With")
        response.headers.add("Access-Control-Allow-Methods", "GET,PUT,POST,DELETE,OPTIONS")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response
    try:
        # Check if file was uploaded
        if 'file' not in request.files:
            error_response = jsonify({
                'error': 'No file uploaded',
                'is_georeferenced': False
            })
            error_response.headers.add("Access-Control-Allow-Origin", "http://localhost:3000")
            error_response.headers.add("Access-Control-Allow-Credentials", "true")
            return error_response, 400
        
        file = request.files['file']
        if file.filename == '' or not file.filename.lower().endswith('.ifc'):
            error_response = jsonify({
                'error': 'Invalid file. Please upload an IFC file.',
                'is_georeferenced': False
            })
            error_response.headers.add("Access-Control-Allow-Origin", "http://localhost:3000")
            error_response.headers.add("Access-Control-Allow-Credentials", "true")
            return error_response, 400
        
        # Save uploaded file temporarily
        filename = secure_filename(file.filename)
        temp_path = os.path.join(app.config['UPLOAD_FOLDER'], f"temp_{filename}")
        file.save(temp_path)
        
        # Open and analyze IFC file
        ifc_file = ifcopenshell.open(temp_path)
        
        # Initialize comprehensive analysis results
        analysis_result = {
            'filename': filename,
            'schema': ifc_file.schema,
            'is_georeferenced': False,
            'georeferencing_method': None,
            'georeferencing_info': {},
            'spatial_structure': {},
            'coordinates_info': {},
            'world_coordinates': {},
            'mapconversion_data': {},
            'site_reference': {},
            'units_info': {},
            'building_elements': {},
            'issues': [],
            'recommendations': []
        }
        
        print(f"=== ANALYZING GEOREFERENCING FOR {filename} ===")
        print(f"IFC Schema: {ifc_file.schema}")
        
        # Use the existing georef function to determine if file is georeferenced
        message, is_georeferenced = georef(ifc_file)
        analysis_result['is_georeferenced'] = is_georeferenced
        
        print(f"Georef function result: {message}")
        print(f"Is georeferenced: {is_georeferenced}")
        
        if is_georeferenced:
            # Get detailed georeferencing information
            try:
                IfcMapConversion, IfcProjectedCRS = georeference_ifc.get_mapconversion_crs(ifc_file=ifc_file)
                
                if IfcMapConversion is not None and IfcProjectedCRS is not None:
                    # Determine method based on schema
                    if ifc_file.schema[:4] == 'IFC4':
                        analysis_result['georeferencing_method'] = 'IFC4+ MapConversion'
                    elif ifc_file.schema == 'IFC2X3':
                        analysis_result['georeferencing_method'] = 'IFC2x3 Property Sets'
                    else:
                        analysis_result['georeferencing_method'] = 'Unknown Method'
                    
                    print(f"✅ Found georeferencing data using {analysis_result['georeferencing_method']}")
                    
                    # Extract MapConversion information
                    map_conversion = {
                        'eastings': float(IfcMapConversion.Eastings) if hasattr(IfcMapConversion, 'Eastings') and IfcMapConversion.Eastings is not None else None,
                        'northings': float(IfcMapConversion.Northings) if hasattr(IfcMapConversion, 'Northings') and IfcMapConversion.Northings is not None else None,
                        'orthogonal_height': float(IfcMapConversion.OrthogonalHeight) if hasattr(IfcMapConversion, 'OrthogonalHeight') and IfcMapConversion.OrthogonalHeight is not None else None,
                        'x_axis_abscissa': float(IfcMapConversion.XAxisAbscissa) if hasattr(IfcMapConversion, 'XAxisAbscissa') and IfcMapConversion.XAxisAbscissa is not None else 1.0,
                        'x_axis_ordinate': float(IfcMapConversion.XAxisOrdinate) if hasattr(IfcMapConversion, 'XAxisOrdinate') and IfcMapConversion.XAxisOrdinate is not None else 0.0,
                        'scale': float(IfcMapConversion.Scale) if hasattr(IfcMapConversion, 'Scale') and IfcMapConversion.Scale is not None else 1.0
                    }
                    
                    analysis_result['georeferencing_info']['map_conversion'] = map_conversion
                    
                    # Extract ProjectedCRS information
                    projected_crs = {
                        'name': IfcProjectedCRS.Name if hasattr(IfcProjectedCRS, 'Name') and IfcProjectedCRS.Name else 'Unknown CRS',
                        'description': IfcProjectedCRS.Description if hasattr(IfcProjectedCRS, 'Description') and IfcProjectedCRS.Description else None
                    }
                    
                    analysis_result['georeferencing_info']['projected_crs'] = projected_crs
                    
                    # Add mapconversion_data for compatibility
                    analysis_result['mapconversion_data'] = {
                        'target_crs': projected_crs['name'],
                        'eastings': map_conversion['eastings'],
                        'northings': map_conversion['northings'],
                        'orthogonal_height': map_conversion['orthogonal_height'],
                        'scale': map_conversion['scale']
                    }
                    
                    # Calculate rotation
                    rotation_radians = math.atan2(map_conversion['x_axis_ordinate'], map_conversion['x_axis_abscissa'])
                    rotation_degrees = math.degrees(rotation_radians)
                    
                    analysis_result['georeferencing_info']['rotation'] = {
                        'degrees': rotation_degrees,
                        'radians': rotation_radians
                    }
                    
                    # Calculate world coordinates if we have valid coordinates
                    if map_conversion['eastings'] is not None and map_conversion['northings'] is not None:
                        try:
                            # Extract EPSG code if available
                            if ':' in projected_crs['name']:
                                epsg_code = int(projected_crs['name'].split(':')[1])
                                
                                # Convert to WGS84 for lat/lon
                                from pyproj import Transformer
                                transformer = Transformer.from_crs(f"EPSG:{epsg_code}", "EPSG:4326")
                                lat, lon = transformer.transform(map_conversion['eastings'], map_conversion['northings'])
                                
                                analysis_result['world_coordinates'] = {
                                    'utm': {
                                        'x': map_conversion['eastings'],
                                        'y': map_conversion['northings'],
                                        'z': map_conversion['orthogonal_height'] or 0,
                                        'zone': epsg_code
                                    },
                                    'wgs84': {
                                        'latitude': lat,
                                        'longitude': lon,
                                        'altitude': map_conversion['orthogonal_height'] or 0
                                    },
                                    'three_js': {
                                        'x': map_conversion['eastings'] * 0.001,  # Scale for Three.js
                                        'y': (map_conversion['orthogonal_height'] or 0) * 0.001,
                                        'z': map_conversion['northings'] * 0.001,
                                        'scale': map_conversion['scale'],
                                        'rotation_radians': rotation_radians
                                    }
                                }
                                
                                print(f"   Real-world location: {lat:.6f}°N, {lon:.6f}°E")
                                print(f"   UTM coordinates: E={map_conversion['eastings']:.2f}, N={map_conversion['northings']:.2f}")
                                
                        except Exception as e:
                            print(f"Warning: Could not calculate world coordinates: {e}")
                    
            except Exception as e:
                analysis_result['issues'].append({
                    'type': 'error',
                    'message': f'Error extracting georeferencing details: {str(e)}'
                })
                print(f"❌ Error extracting georeferencing details: {str(e)}")
        
        # Check for site reference information (IFC2x3 and fallback)
        try:
            ifc_sites = ifc_file.by_type("IfcSite")
            if ifc_sites:
                site = ifc_sites[0]
                
                site_ref = {
                    'name': site.Name if hasattr(site, 'Name') and site.Name else None,
                    'description': site.Description if hasattr(site, 'Description') and site.Description else None,
                    'ref_latitude': None,
                    'ref_longitude': None,
                    'ref_elevation': None,
                    'local_placement': None
                }
                
                # Check RefLatitude and RefLongitude
                if hasattr(site, 'RefLatitude') and site.RefLatitude is not None:
                    lat_components = site.RefLatitude
                    if len(lat_components) >= 3:
                        lat = float(lat_components[0]) + float(lat_components[1])/60 + float(lat_components[2] + (lat_components[3] if len(lat_components) > 3 else 0)/1000000)/(60*60)
                        site_ref['ref_latitude'] = lat
                
                if hasattr(site, 'RefLongitude') and site.RefLongitude is not None:
                    lon_components = site.RefLongitude
                    if len(lon_components) >= 3:
                        lon = float(lon_components[0]) + float(lon_components[1])/60 + float(lon_components[2] + (lon_components[3] if len(lon_components) > 3 else 0)/1000000)/(60*60)
                        site_ref['ref_longitude'] = lon
                
                if hasattr(site, 'RefElevation') and site.RefElevation is not None:
                    site_ref['ref_elevation'] = float(site.RefElevation)
                
                # Check local placement
                if hasattr(site, 'ObjectPlacement') and site.ObjectPlacement:
                    try:
                        matrix = ifcopenshell.util.placement.get_local_placement(site.ObjectPlacement)
                        site_ref['local_placement'] = {
                            'x': float(matrix[0][3]),
                            'y': float(matrix[1][3]), 
                            'z': float(matrix[2][3])
                        }
                    except:
                        pass
                
                analysis_result['site_reference'] = site_ref
                
        except Exception as e:
            print(f"Warning: Error checking site reference: {e}")
        
        # Analyze units
        try:
            unit_assignments = ifc_file.by_type('IfcUnitAssignment')
            if unit_assignments:
                units = unit_assignments[0].Units
                units_info = {
                    'length_unit': None,
                    'area_unit': None, 
                    'volume_unit': None,
                    'angle_unit': None
                }
                
                for unit in units:
                    if unit.is_a('IfcSIUnit'):
                        unit_type = unit.UnitType
                        unit_name = unit.Name
                        if hasattr(unit, 'Prefix') and unit.Prefix:
                            unit_name = unit.Prefix + unit_name
                        
                        if unit_type == 'LENGTHUNIT':
                            units_info['length_unit'] = unit_name
                        elif unit_type == 'AREAUNIT':
                            units_info['area_unit'] = unit_name
                        elif unit_type == 'VOLUMEUNIT':
                            units_info['volume_unit'] = unit_name
                        elif unit_type == 'PLANEANGLEUNIT':
                            units_info['angle_unit'] = unit_name
                
                analysis_result['units_info'] = units_info
                
        except Exception as e:
            print(f"Warning: Error analyzing units: {e}")
        
        # Analyze spatial structure
        try:
            projects = ifc_file.by_type('IfcProject')
            buildings = ifc_file.by_type('IfcBuilding')
            building_storeys = ifc_file.by_type('IfcBuildingStorey')
            spaces = ifc_file.by_type('IfcSpace')
            sites = ifc_file.by_type('IfcSite')
            
            analysis_result['spatial_structure'] = {
                'projects': len(projects),
                'sites': len(sites),
                'buildings': len(buildings),
                'building_storeys': len(building_storeys),
                'spaces': len(spaces)
            }
            
        except Exception as e:
            print(f"Warning: Error analyzing spatial structure: {e}")
        
        # Analyze building elements and coordinates
        try:
            products = ifc_file.by_type('IfcProduct')
            coordinates = []
            element_types = {}
            
            for product in products:
                # Count element types
                element_type = product.is_a()
                element_types[element_type] = element_types.get(element_type, 0) + 1
                
                # Extract coordinates
                if product.Representation and product.ObjectPlacement:
                    try:
                        matrix = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
                        x, y, z = matrix[0][3], matrix[1][3], matrix[2][3]
                        coordinates.append([float(x), float(y), float(z)])
                    except:
                        continue
            
            if coordinates:
                import numpy as np
                coords_array = np.array(coordinates)
                
                analysis_result['coordinates_info'] = {
                    'total_objects': len(coordinates),
                    'bounds': {
                        'min_x': float(coords_array[:, 0].min()),
                        'max_x': float(coords_array[:, 0].max()),
                        'min_y': float(coords_array[:, 1].min()),
                        'max_y': float(coords_array[:, 1].max()),
                        'min_z': float(coords_array[:, 2].min()),
                        'max_z': float(coords_array[:, 2].max())
                    },
                    'dimensions': {
                        'width': float(coords_array[:, 0].max() - coords_array[:, 0].min()),
                        'depth': float(coords_array[:, 1].max() - coords_array[:, 1].min()),
                        'height': float(coords_array[:, 2].max() - coords_array[:, 2].min())
                    },
                    'center': {
                        'x': float(coords_array[:, 0].mean()),
                        'y': float(coords_array[:, 1].mean()),
                        'z': float(coords_array[:, 2].mean())
                    }
                }
            
            # Building elements summary
            analysis_result['building_elements'] = {
                'total_products': len(products),
                'element_types': element_types,
                'top_elements': sorted(element_types.items(), key=lambda x: x[1], reverse=True)[:10]
            }
            
        except Exception as e:
            print(f"Warning: Error analyzing coordinates: {e}")
        
        # Generate recommendations
        if not is_georeferenced:
            analysis_result['recommendations'].append({
                'type': 'error',
                'message': 'File is not georeferenced. Consider adding coordinate reference system information.'
            })
        else:
            analysis_result['recommendations'].append({
                'type': 'success',
                'message': f"File is properly georeferenced using {analysis_result['georeferencing_method']}"
            })
        
        # Add bounding box in easting/northing coordinates if georeferenced
        if is_georeferenced and 'coordinates_info' in analysis_result and analysis_result['coordinates_info'] and 'georeferencing_info' in analysis_result:
            try:
                bounds = analysis_result['coordinates_info']['bounds']
                map_conv = analysis_result['georeferencing_info'].get('map_conversion', {})
                proj_crs = analysis_result['georeferencing_info'].get('projected_crs', {})
                
                eastings = map_conv.get('eastings', 0)
                northings = map_conv.get('northings', 0)
                orth_height = map_conv.get('orthogonal_height', 0)
                x_axis_abscissa = map_conv.get('x_axis_abscissa', 1.0)
                x_axis_ordinate = map_conv.get('x_axis_ordinate', 0.0)
                scale = map_conv.get('scale', 1.0)
                
                # Get CRS EPSG code for coordinate transformation
                crs_name = proj_crs.get('name', 'EPSG:2056')
                epsg_code = None
                if ':' in crs_name:
                    try:
                        epsg_code = int(crs_name.split(':')[1])
                    except:
                        epsg_code = 2056
                else:
                    epsg_code = 2056
                
                # Create transformer for world coords to WGS84 (lat/lon)
                transformer = None
                try:
                    transformer = Transformer.from_crs(f"EPSG:{epsg_code}", "EPSG:4326", always_xy=True)
                except Exception as e:
                    print(f"Warning: Could not create coordinate transformer: {e}")
                
                # Calculate 4 corners of bounding box at ground level
                corners_local = [
                    {'x': bounds['min_x'], 'y': bounds['min_y'], 'z': bounds['min_z'], 'name': 'bottom_left'},
                    {'x': bounds['max_x'], 'y': bounds['min_y'], 'z': bounds['min_z'], 'name': 'bottom_right'},
                    {'x': bounds['max_x'], 'y': bounds['max_y'], 'z': bounds['min_z'], 'name': 'top_right'},
                    {'x': bounds['min_x'], 'y': bounds['max_y'], 'z': bounds['min_z'], 'name': 'top_left'}
                ]
                
                # Transform to world coordinates (easting/northing)
                bbox_world_corners = []
                for corner in corners_local:
                    local_x = corner['x']
                    local_y = corner['y']
                    local_z = corner['z']
                    
                    # Apply rotation to local coordinates
                    if x_axis_abscissa and x_axis_ordinate:
                        angle = math.atan2(x_axis_ordinate, x_axis_abscissa)
                        cos_a = math.cos(angle)
                        sin_a = math.sin(angle)
                        rotated_x = local_x * cos_a - local_y * sin_a
                        rotated_y = local_x * sin_a + local_y * cos_a
                        local_x, local_y = rotated_x, rotated_y
                    
                    # Apply scale
                    if scale and scale != 1.0:
                        local_x *= scale
                        local_y *= scale
                        local_z *= scale
                    
                    # Add eastings/northings
                    world_e = local_x + eastings
                    world_n = local_y + northings
                    world_h = local_z + (orth_height if orth_height else 0)
                    
                    # Convert to WGS84 lat/lon
                    lat, lon = None, None
                    if transformer:
                        try:
                            lon, lat = transformer.transform(world_e, world_n)
                        except Exception as e:
                            print(f"Warning: Could not transform corner {corner['name']}: {e}")
                    
                    bbox_world_corners.append({
                        'name': corner['name'],
                        'easting': world_e,
                        'northing': world_n,
                        'elevation': world_h,
                        'latitude': lat,
                        'longitude': lon
                    })
                
                # Calculate min/max corners in lat/lon
                min_corner_latlon = None
                max_corner_latlon = None
                if transformer:
                    try:
                        min_e = min(c['easting'] for c in bbox_world_corners)
                        min_n = min(c['northing'] for c in bbox_world_corners)
                        max_e = max(c['easting'] for c in bbox_world_corners)
                        max_n = max(c['northing'] for c in bbox_world_corners)
                        
                        min_lon, min_lat = transformer.transform(min_e, min_n)
                        max_lon, max_lat = transformer.transform(max_e, max_n)
                        
                        min_corner_latlon = {'latitude': min_lat, 'longitude': min_lon}
                        max_corner_latlon = {'latitude': max_lat, 'longitude': max_lon}
                    except Exception as e:
                        print(f"Warning: Could not transform min/max corners: {e}")
                
                # Add to analysis result
                analysis_result['bounding_box_world'] = {
                    'corners': bbox_world_corners,
                    'min': {
                        'easting': min(c['easting'] for c in bbox_world_corners),
                        'northing': min(c['northing'] for c in bbox_world_corners),
                        'elevation': bounds['min_z'] + (orth_height if orth_height else 0),
                        'latitude': min_corner_latlon['latitude'] if min_corner_latlon else None,
                        'longitude': min_corner_latlon['longitude'] if min_corner_latlon else None
                    },
                    'max': {
                        'easting': max(c['easting'] for c in bbox_world_corners),
                        'northing': max(c['northing'] for c in bbox_world_corners),
                        'elevation': bounds['max_z'] + (orth_height if orth_height else 0),
                        'latitude': max_corner_latlon['latitude'] if max_corner_latlon else None,
                        'longitude': max_corner_latlon['longitude'] if max_corner_latlon else None
                    },
                    'crs': crs_name,
                    'description': 'Bounding box of the IFC building in world coordinates (Easting/Northing) and WGS84 (Lat/Long)'
                }
                
            except Exception as e:
                print(f"Warning: Error calculating bounding box in world coordinates: {e}")
        
        # Clean up temporary file
        if os.path.exists(temp_path):
            os.remove(temp_path)
        
        print("==========================================")
        response = jsonify(analysis_result)
        response.headers.add("Access-Control-Allow-Origin", "http://localhost:3000")
        response.headers.add("Access-Control-Allow-Credentials", "true")
        return response
        
    except Exception as e:
        # Clean up temporary file on error
        if 'temp_path' in locals() and os.path.exists(temp_path):
            os.remove(temp_path)
            
        error_msg = f'Error analyzing IFC file: {str(e)}'
        print(f"❌ {error_msg}")
        error_response = jsonify({
            'error': error_msg,
            'is_georeferenced': False
        })
        error_response.headers.add("Access-Control-Allow-Origin", "http://localhost:3000")
        error_response.headers.add("Access-Control-Allow-Credentials", "true")
        return error_response, 500

@app.route('/api/ifc-analysis/<filename>')
def analyze_ifc_api(filename):
    """API endpoint to analyze IFC file and return coordinate information"""
    try:
        fn = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if not os.path.exists(fn):
            return {'error': 'File not found'}, 404
        
        ifc_file = ifcopenshell.open(fn)
        
        # Get basic model info
        schema = ifc_file.schema
        products = ifc_file.by_type('IfcProduct')
        
        # Extract coordinates from all products
        coordinates = []
        for product in products:
            if product.Representation and product.ObjectPlacement:
                try:
                    matrix = ifcopenshell.util.placement.get_local_placement(product.ObjectPlacement)
                    x, y, z = matrix[0][3], matrix[1][3], matrix[2][3]
                    coordinates.append([float(x), float(y), float(z)])
                except:
                    continue
        
        if coordinates:
            import numpy as np
            coords_array = np.array(coordinates)
            min_coords = np.min(coords_array, axis=0).tolist()
            max_coords = np.max(coords_array, axis=0).tolist()
            center_coords = np.mean(coords_array, axis=0).tolist()
            dimensions = (coords_array.max(axis=0) - coords_array.min(axis=0)).tolist()
        else:
            min_coords = max_coords = center_coords = dimensions = [0, 0, 0]
        
        return {
            'filename': filename,
            'schema': schema,
            'product_count': len(products),
            'coordinates_count': len(coordinates),
            'bounds': {
                'min': min_coords,
                'max': max_coords,
                'center': center_coords,
                'dimensions': dimensions
            },
            'coordinates': coordinates[:100] if len(coordinates) > 100 else coordinates  # Limit for performance
        }
        
    except Exception as e:
        return {'error': str(e)}, 500

@app.route('/api/ifc-geometry/<filename>')
def get_ifc_geometry(filename):
    """API endpoint to extract IFC geometry for Three.js visualization"""
    try:
        fn = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if not os.path.exists(fn):
            return {'error': 'File not found'}, 404
        
        ifc_file = ifcopenshell.open(fn)
        
        # Settings for geometry processing
        settings = ifcopenshell.geom.settings()
        settings.set(settings.USE_WORLD_COORDS, True)
        settings.set(settings.DISABLE_OPENING_SUBTRACTIONS, False)
        
        # Get all geometric products
        products = ifc_file.by_type('IfcProduct')
        
        geometries = []
        all_vertices = []
        
        for product in products:
            # Skip IfcSpace and other non-visual elements
            if product.is_a('IfcSpace') or product.is_a('IfcOpeningElement'):
                continue
                
            if product.Representation:
                try:
                    shape = ifcopenshell.geom.create_shape(settings, product)
                    verts = shape.geometry.verts
                    faces = shape.geometry.faces
                    
                    # Convert to format suitable for Three.js
                    vertices = []
                    for i in range(0, len(verts), 3):
                        vertices.extend([verts[i], verts[i+1], verts[i+2]])
                        all_vertices.append([verts[i], verts[i+1], verts[i+2]])
                    
                    indices = list(faces)
                    
                    # Get material/style info
                    color = '#888888'  # default gray
                    if hasattr(product, 'Representation'):
                        # Try to extract color if available
                        pass
                    
                    geometries.append({
                        'vertices': vertices,
                        'indices': indices,
                        'color': color,
                        'type': product.is_a()
                    })
                    
                except Exception as e:
                    print(f"Error processing {product.is_a()}: {e}")
                    continue
        
        # Calculate bounds from all vertices
        if all_vertices:
            import numpy as np
            coords_array = np.array(all_vertices)
            min_coords = np.min(coords_array, axis=0).tolist()
            max_coords = np.max(coords_array, axis=0).tolist()
            center_coords = np.mean(coords_array, axis=0).tolist()
            dimensions = (coords_array.max(axis=0) - coords_array.min(axis=0)).tolist()
        else:
            min_coords = max_coords = center_coords = dimensions = [0, 0, 0]
        
        return jsonify({
            'geometries': geometries,
            'bounds': {
                'min': min_coords,
                'max': max_coords,
                'center': center_coords,
                'dimensions': dimensions
            }
        })
        
    except Exception as e:
        import traceback
        traceback.print_exc()
        return {'error': str(e)}, 500

@app.route('/api/upload-to-project/<project_id>', methods=['POST'])
def upload_to_project(project_id):
    """Proxy endpoint to upload files to external API to avoid CORS issues"""
    import requests
    
    try:
        # Get the file from the download endpoint
        filename = request.json.get('filename') if request.is_json else request.form.get('filename')
        auth_token = request.headers.get('Authorization')
        
        if not filename:
            return jsonify({'error': 'Filename is required'}), 400
            
        # Check if file exists
        file_path = os.path.join(app.config['UPLOAD_FOLDER'], filename)
        if not os.path.exists(file_path):
            return jsonify({'error': 'File not found'}), 404
            
        # Prepare the file for upload
        with open(file_path, 'rb') as f:
            files = {'files': (filename, f, 'application/octet-stream')}
            
            # Prepare headers
            headers = {}
            if auth_token:
                headers['Authorization'] = auth_token
                
            # Make request to external API
            external_url = f'{API_BASE_URL}/api/ifc/{project_id}/upload/'
            
            response = requests.post(
                external_url,
                files=files,
                headers=headers,
                timeout=60
            )
            
            # Return the response from external API
            if response.ok:
                return jsonify(response.json()), response.status_code
            else:
                return jsonify({
                    'error': f'Upload failed: {response.status_code}',
                    'message': response.text
                }), response.status_code
                
    except requests.exceptions.RequestException as e:
        return jsonify({'error': f'Network error: {str(e)}'}), 500
    except Exception as e:
        return jsonify({'error': f'Server error: {str(e)}'}), 500

if __name__ == '__main__':
    app.run(debug=True, port=5002)
