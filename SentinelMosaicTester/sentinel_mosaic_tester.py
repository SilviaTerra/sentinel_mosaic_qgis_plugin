# -*- coding: utf-8 -*-
"""
/***************************************************************************
 SentinelMosaicTester
                                 A QGIS plugin
 This plugin orders low resolution Sentinel mosaic images
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                              -------------------
        begin                : 2021-03-03
        git sha              : $Format:%H$
        copyright            : (C) 2021 by SilviaTerra
        email                : henry@silviaterra.com
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
"""
from qgis.PyQt.QtCore import QSettings, QTranslator, QCoreApplication, Qt
from qgis.PyQt.QtGui import QIcon
from qgis.PyQt.QtWidgets import QAction, QProgressBar
# Initialize Qt resources from file resources.py
from .resources import *

# Import the code for the DockWidget
from .sentinel_mosaic_tester_dockwidget import SentinelMosaicTesterDockWidget
import os.path

from qgis.core import (
    Qgis,
    QgsCoordinateReferenceSystem,
    QgsCoordinateTransform,
    QgsProject,
    QgsRectangle,
    QgsPointXY,
    QgsGeometry,
    QgsVectorLayer,
    QgsFeature,
    QgsMessageLog
    )

import numpy as np
import parse
import re
import time

import datetime as dt

from sentinelhub import BBox, CRS, DataCollection, \
    Geometry, get_image_dimension, MimeType, SentinelHubRequest, \
    SHConfig, WebFeatureService

# assumes sentinelhub authentication is set via sentinelhub.config
config = SHConfig()

S2_GRANULE_ID_FMT = (
    'S{sat}_{file_class}_{file_category}_' +
    '{level}_{descriptor}_{site_centre}_' +
    '{creation_date}_A{absolute_orbit}_' +
    'T{tile}_{processing_baseline}'
)

PREVIEW_EVALSCRIPT = """
//VERSION=3

// based on this evalscript:
// https://github.com/sentinel-hub/custom-scripts/blob/master/sentinel-2/cloudless_mosaic/L2A-first_quartile_4bands.js

function setup() {
  return {
    input: [{
      bands: [
        "B08", // near infrared
        "B03", // green
        "B02", // blue
        "SCL" // pixel classification
      ],
      units: "DN"
    }],
    output: [
      {
        id: "default",
        bands: 3,
        sampleType: SampleType.UINT16
      }
    ],
    mosaicking: "ORBIT"
  };
}

// acceptable images are ones collected on specified dates
function filterScenes(availableScenes, inputMetadata) {
  var allowedDates = [%s]; // format with python
  return availableScenes.filter(function (scene) {
    var sceneDateStr = scene.date.toISOString().split("T")[0]; //converting date and time to string and rounding to day precision
    return allowedDates.includes(sceneDateStr);
  });
}

function getValue(values) {
  values.sort(function (a, b) {
    return a - b;
  });
  return getMedian(values);
}

// function for pulling median (second quartile) of values
function getMedian(sortedValues) {
  var index = Math.floor(sortedValues.length / 2);
  return sortedValues[index];
}

function validate(samples) {
  var scl = samples.SCL;

  if (scl === 3) { // SC_CLOUD_SHADOW
    return false;
  } else if (scl === 9) { // SC_CLOUD_HIGH_PROBA
    return false;
  } else if (scl === 8) { // SC_CLOUD_MEDIUM_PROBA
    return false;
  } else if (scl === 7) { // SC_CLOUD_LOW_PROBA
    // return false;
  } else if (scl === 10) { // SC_THIN_CIRRUS
    return false;
  } else if (scl === 11) { // SC_SNOW_ICE
    return false;
  } else if (scl === 1) { // SC_SATURATED_DEFECTIVE
    return false;
  } else if (scl === 2) { // SC_DARK_FEATURE_SHADOW
    // return false;
  }
  return true;
}

function evaluatePixel(samples, scenes) {
  var clo_b02 = [];
  var clo_b03 = [];
  var clo_b08 = [];
  var clo_b02_invalid = [];
  var clo_b03_invalid = [];
  var clo_b08_invalid = [];
  var a = 0;
  var a_invalid = 0;

  for (var i = 0; i < samples.length; i++) {
    var sample = samples[i];
    if (sample.B02 > 0 && sample.B03 > 0 && sample.B08 > 0) {
      var isValid = validate(sample);

      if (isValid) {
        clo_b02[a] = sample.B02;
        clo_b03[a] = sample.B03;
        clo_b08[a] = sample.B08;
        a = a + 1;
      } else {
        clo_b02_invalid[a_invalid] = sample.B02;
        clo_b03_invalid[a_invalid] = sample.B03;
        clo_b08_invalid[a_invalid] = sample.B08;
        a_invalid = a_invalid + 1;
      }
    }
  }

  var gValue;
  var bValue;
  var nValue;
  if (a > 0) {
    gValue = getValue(clo_b03);
    bValue = getValue(clo_b02);
    nValue = getValue(clo_b08);
  } else if (a_invalid > 0) {
    gValue = getValue(clo_b03_invalid);
    bValue = getValue(clo_b02_invalid);
    nValue = getValue(clo_b08_invalid);
  } else {
    gValue = 0;
    bValue = 0;
    nValue = 0;
  }
  return {
    default: [nValue, gValue, bValue]
  };
}
"""


def absolute_to_relative_orbit(absolute_orbit, sat):
    '''
    Translate Sentinel 2 absolute orbit number to relative orbit number. There
    are 143 relative orbits that are similar to Landsat paths. The relative
    orbit numbers are not readily visible in some Sentinel 2 product IDs so we
    must convert from absolute (number of orbits since some origin point in
    time) to relative orbits (number of orbits since orbit 1).
    '''
    assert sat in ['2A', '2B']
    if sat == '2A':
        adj = -140
    if sat == '2B':
        adj = -26

    return (absolute_orbit + adj) % 143


def get_dates_by_orbit(bbox, start_date, end_date, max_cc, target_orbit, config):
    '''
    For a given bounding box, query Sentinel 2 imagery collection dates between
    two dates (start/end_date) that match a specified list of relative orbits
    and have a maximum cloud cover proportion.

    * bbox is a WGS84 bounding box created by sentinelhub.Geometry.BBox
    * start_date and end_date are date strings formatted as yyyy-mm-dd
    * max_cc is the maximum allowed cloud cover (0-1 scale)
    * target_orbit is a list containing relative orbit numbers to be included
    * config is the Sentinel Hub config object created by sentinelhub.SHConfig()
    '''
    assert target_orbit is not None, "target_orbit must be specified"

    # convert target_orbit to list if just a single orbit
    if type(target_orbit) is int:
        target_orbit = [target_orbit]

    # define time window
    search_time_interval = (f'{start_date}T00:00:00', f'{end_date}T23:59:59')

    # query scenes
    wfs_iterator = WebFeatureService(
        bbox,
        search_time_interval,
        data_collection=DataCollection.SENTINEL2_L2A,
        maxcc=max_cc,
        config=config
    )

    # filter down to dates from specified orbit(s)
    dates = []
    for tile_info in wfs_iterator:
        # raw product ID
        product_id = tile_info['properties']['id']

        # parse the product ID
        product_vals = parse.parse(S2_GRANULE_ID_FMT, product_id)

        # acquisition date
        date = tile_info['properties']['date']

        # absolute orbit is buried in ID after _A string
        absolute_orbit = int(product_vals['absolute_orbit'])

        # which satellite? 2A or 2B
        sat = product_vals['sat']
        assert sat in ('2A', '2B')

        # convert to relative orbit
        relative_orbit = absolute_to_relative_orbit(absolute_orbit, sat)

        if relative_orbit not in target_orbit:
            continue

        # add date if not already added to list
        if date not in dates:
            dates.append(date)

    assert len(dates) > 0, \
        f'No dates available for this bounding box and relative orbit {target_orbit}'

    return dates


def filter_dates(dates, months, years):
    '''
    Filter a list of dates (yyyy-mm-dd format) to only include dates from a list
    of months and years
    '''
    # convert date strings to date objects
    dates = [dt.datetime.strptime(date, '%Y-%m-%d').date() for date in dates]

    # filter down to supplied months/years
    filtered = [date.strftime(
        '%Y-%m-%d') for date in dates if date.month in months and date.year in years]

    assert len(filtered) > 0, \
        'None of supplied dates satisfy desired months/years'

    return filtered

class SentinelMosaicTester:
    """QGIS Plugin Implementation."""

    def __init__(self, iface):
        """Constructor.

        :param iface: An interface instance that will be passed to this class
            which provides the hook by which you can manipulate the QGIS
            application at run time.
        :type iface: QgsInterface
        """
        # Save reference to the QGIS interface
        self.iface = iface

        # initialize plugin directory
        self.plugin_dir = os.path.dirname(__file__)

        # initialize locale
        locale = QSettings().value('locale/userLocale')[0:2]
        locale_path = os.path.join(
            self.plugin_dir,
            'i18n',
            'SentinelMosaicTester_{}.qm'.format(locale))

        if os.path.exists(locale_path):
            self.translator = QTranslator()
            self.translator.load(locale_path)
            QCoreApplication.installTranslator(self.translator)

        # Declare instance attributes
        self.actions = []
        self.menu = self.tr(u'&SentinelMosaicTester')
        # TODO: We are going to let the user set this up in a future iteration
        self.toolbar = self.iface.addToolBar(u'SentinelMosaicTester')
        self.toolbar.setObjectName(u'SentinelMosaicTester')

        #print "** INITIALIZING SentinelMosaicTester"

        self.pluginIsActive = False
        self.dockwidget = None
        self.first_start = None


    # noinspection PyMethodMayBeStatic
    def tr(self, message):
        """Get the translation for a string using Qt translation API.

        We implement this ourselves since we do not inherit QObject.

        :param message: String for translation.
        :type message: str, QString

        :returns: Translated version of message.
        :rtype: QString
        """
        # noinspection PyTypeChecker,PyArgumentList,PyCallByClass
        return QCoreApplication.translate('SentinelMosaicTester', message)


    def add_action(
        self,
        icon_path,
        text,
        callback,
        enabled_flag=True,
        add_to_menu=True,
        add_to_toolbar=True,
        status_tip=None,
        whats_this=None,
        parent=None):
        """Add a toolbar icon to the toolbar.

        :param icon_path: Path to the icon for this action. Can be a resource
            path (e.g. ':/plugins/foo/bar.png') or a normal file system path.
        :type icon_path: str

        :param text: Text that should be shown in menu items for this action.
        :type text: str

        :param callback: Function to be called when the action is triggered.
        :type callback: function

        :param enabled_flag: A flag indicating if the action should be enabled
            by default. Defaults to True.
        :type enabled_flag: bool

        :param add_to_menu: Flag indicating whether the action should also
            be added to the menu. Defaults to True.
        :type add_to_menu: bool

        :param add_to_toolbar: Flag indicating whether the action should also
            be added to the toolbar. Defaults to True.
        :type add_to_toolbar: bool

        :param status_tip: Optional text to show in a popup when mouse pointer
            hovers over the action.
        :type status_tip: str

        :param parent: Parent widget for the new action. Defaults None.
        :type parent: QWidget

        :param whats_this: Optional text to show in the status bar when the
            mouse pointer hovers over the action.

        :returns: The action that was created. Note that the action is also
            added to self.actions list.
        :rtype: QAction
        """

        icon = QIcon(icon_path)
        action = QAction(icon, text, parent)
        action.triggered.connect(callback)
        action.setEnabled(enabled_flag)

        if status_tip is not None:
            action.setStatusTip(status_tip)

        if whats_this is not None:
            action.setWhatsThis(whats_this)

        if add_to_toolbar:
            self.toolbar.addAction(action)

        if add_to_menu:
            self.iface.addPluginToMenu(
                self.menu,
                action)

        self.actions.append(action)

        return action


    def initGui(self):
        """Create the menu entries and toolbar icons inside the QGIS GUI."""
        icon_path = '/Users/natasharavinand/Library/Application Support/QGIS/QGIS3/profiles/default/python/plugins/SentinelMosaicTester/icon.png'
        self.add_action(
            icon_path,
            text=self.tr(u'Get Mosaic'),
            callback=self.run,
            parent=self.iface.mainWindow())

    #--------------------------------------------------------------------------

    def onClosePlugin(self):
        """Cleanup necessary items here when plugin dockwidget is closed"""

        #print "** CLOSING SentinelMosaicTester"

        # disconnects
        self.dockwidget.closingPlugin.disconnect(self.onClosePlugin)

        # remove this statement if dockwidget is to remain
        # for reuse if plugin is reopened
        # Commented next statement since it causes QGIS crashe
        # when closing the docked window:
        # self.dockwidget = None

        self.pluginIsActive = False


    def unload(self):
        """Removes the plugin menu item and icon from QGIS GUI."""

        #print "** UNLOAD SentinelMosaicTester"

        for action in self.actions:
            self.iface.removePluginMenu(
                self.tr(u'&SentinelMosaicTester'),
                action)
            self.iface.removeToolBarIcon(action)
        # remove the toolbar
        del self.toolbar

    #--------------------------------------------------------------------------
    # for default evalscript code
    def run_default(self):

        progressMessageBar = self.iface.messageBar().createMessage('Getting mosaic preview')
        progress = QProgressBar()
        progress.setMaximum(3)
        progress.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        progressMessageBar.layout().addWidget(progress)
        self.iface.messageBar().pushWidget(progressMessageBar, Qgis.Info)

        # get bounding box from selected layer
        layer = self.dockwidget.default_selected_layer.currentLayer()
        src_crs = layer.crs()
        layer_extent = layer.extent()
        if src_crs != QgsCoordinateReferenceSystem('EPSG:4326'):
            transform = QgsCoordinateTransform(
                src_crs, QgsCoordinateReferenceSystem('EPSG:4326'), QgsProject.instance())
            layer_extent = transform.transformBoundingBox(layer_extent)
        
        min_x = layer_extent.xMinimum()
        min_y = layer_extent.yMinimum()
        max_x = layer_extent.xMaximum()
        max_y = layer_extent.yMaximum()
        
        QgsMessageLog.logMessage(
            f'bounding box: {min_x}, {min_y}, {max_x}, {max_y}',
            level=Qgis.Info
            )
        
        bbox = BBox(bbox=[min_x, min_y, max_x, max_y], crs=CRS.WGS84)

        # make list of relative orbits
        orbit_string = self.dockwidget.relative_orbit.text()
        orbits = [int(orbit) for orbit in orbit_string.split(',')]
        orbit_list_string = ' & '.join([str(x) for x in orbits])
        QgsMessageLog.logMessage(
            f'orbits: {orbit_list_string}',
            level=Qgis.Info
            )

        # filter down to desired months/years
        months = []
        if self.dockwidget.month_january.isChecked():
            months.append(1)
        if self.dockwidget.month_february.isChecked():
            months.append(2)
        if self.dockwidget.month_march.isChecked():
            months.append(3)
        if self.dockwidget.month_april.isChecked():
            months.append(4)
        if self.dockwidget.month_may.isChecked():
            months.append(5)
        if self.dockwidget.month_june.isChecked():
            months.append(6)
        if self.dockwidget.month_july.isChecked():
            months.append(7)
        if self.dockwidget.month_august.isChecked():
            months.append(8)
        if self.dockwidget.month_september.isChecked():
            months.append(9)
        if self.dockwidget.month_october.isChecked():
            months.append(10)
        if self.dockwidget.month_november.isChecked():
            months.append(11)
        if self.dockwidget.month_december.isChecked():
            months.append(12)
        
        month_string = ' & '.join([str(x) for x in months])
        QgsMessageLog.logMessage(
            f'months: {month_string}',
            level=Qgis.Info
            )

        years = []
        if self.dockwidget.year_2018.isChecked():
            years.append(2018)
        if self.dockwidget.year_2019.isChecked():
            years.append(2019)
        if self.dockwidget.year_2020.isChecked():
            years.append(2020)
        if self.dockwidget.year_2021.isChecked():
            years.append(2021)
        
        year_string = ' & '.join([str(x) for x in years])
        QgsMessageLog.logMessage(
            f'years: {year_string}',
            level=Qgis.Info
            )
        progress.setValue(1)

        # date range for mosaicing
        max_cc = float(self.dockwidget.default_max_cc.text())
        # validate max_cc input
        if max_cc < 0 or max_cc > 1:
            raise ValueError("Please enter a max cloud cover proportion between 0 and 1.")

        first_year, last_year = str(min(years)), str(max(years))
        start_date, end_date = f'{first_year}-01-01', f'{last_year}-12-30'

        # query dates
        QgsMessageLog.logMessage(
            'querying dates for bbox',
            level=Qgis.Info
            )

        dates = get_dates_by_orbit(
            bbox,
            start_date=start_date,
            end_date=end_date,
            max_cc=max_cc,
            target_orbit=orbits,
            config=config)
        
        progress.setValue(2)

        # filter dates down to desired months/years
        dates_filt = filter_dates(dates, months=months, years=years)

        # add dates to evalscript
        date_string = ', '.join([f'"{date}"' for date in dates_filt])
        preview_eval = PREVIEW_EVALSCRIPT % date_string

        # set time interval
        time_interval = [dt.datetime.strptime(
            date, '%Y-%m-%d').date() for date in [start_date, end_date]]
        
        QgsMessageLog.logMessage(
            'requesting preview image',
            level=Qgis.Info
            )

        preview_request = SentinelHubRequest(
            evalscript=preview_eval,
            data_folder='/tmp/mosaic_tests',
            input_data=[
                SentinelHubRequest.input_data(
                    data_collection=DataCollection.SENTINEL2_L2A,
                    time_interval=time_interval,
                    maxcc=max_cc
                )
            ],
            responses=[
                SentinelHubRequest.output_response('default', MimeType.TIFF)
            ],
            bbox=bbox,
            size=(512, get_image_dimension(bbox=bbox, width=512)),
            config=config
        )

        preview_request.get_data(save_data=True)
        progress.setValue(3)
        output_file = '/tmp/mosaic_tests' + '/' + preview_request.get_filename_list()[0]
        
        # add file to QGIS
        layer_name = ' '.join(
            ['orbits:', orbit_list_string, 'months:', month_string, 'years:', year_string])
        self.iface.addRasterLayer(output_file, layer_name)
        self.iface.messageBar().clearWidgets()

        return None

    # for custom evalscript code
    def run_custom_evalscript(self):

        progressMessageBar = self.iface.messageBar().createMessage('Getting mosaic preview')
        progress = QProgressBar()
        progress.setMaximum(3)
        progress.setAlignment(Qt.AlignLeft | Qt.AlignVCenter)
        progressMessageBar.layout().addWidget(progress)
        self.iface.messageBar().pushWidget(progressMessageBar, Qgis.Info)

        # get bounding box from selected layer
        layer = self.dockwidget.custom_selected_layer.currentLayer()
        src_crs = layer.crs()
        layer_extent = layer.extent()
        if src_crs != QgsCoordinateReferenceSystem('EPSG:4326'):
            transform = QgsCoordinateTransform(
                src_crs, QgsCoordinateReferenceSystem('EPSG:4326'), QgsProject.instance())
            layer_extent = transform.transformBoundingBox(layer_extent)
        
        min_x = layer_extent.xMinimum()
        min_y = layer_extent.yMinimum()
        max_x = layer_extent.xMaximum()
        max_y = layer_extent.yMaximum()
        
        QgsMessageLog.logMessage(
            f'bounding box: {min_x}, {min_y}, {max_x}, {max_y}',
            level=Qgis.Info
            )
        
        bbox = BBox(bbox=[min_x, min_y, max_x, max_y], crs=CRS.WGS84)

        # validate and set time interval
        start_date, end_date = self.dockwidget.start_date.text(), self.dockwidget.end_date.text()
        proper_date_format = "%Y-%m-%d"
        try:
            dt.datetime.strptime(start_date, proper_date_format)
            dt.datetime.strptime(end_date, proper_date_format)
        except ValueError:
            raise ValueError("Please enter start and end dates with the format of YYYY-MM-DD (ex. 2000-01-01).")
        
        time_interval = [start_date, end_date]

        # set and validate max_cc
        max_cc = float(self.dockwidget.custom_max_cc.text())

        if max_cc < 0 or max_cc > 1:
            raise ValueError("Please enter a max cloud cover proportion between 0 and 1.")

        # grab user inputted custom evalscript and substitute generic
        custom_evalscript_code = self.dockwidget.custom_evalscript_code.toPlainText()
        preview_eval = custom_evalscript_code
        
        QgsMessageLog.logMessage(
            'requesting preview image',
            level=Qgis.Info
            )
        
        preview_request = SentinelHubRequest(
            evalscript=preview_eval,
            data_folder='/tmp/mosaic_tests',
            input_data=[
                SentinelHubRequest.input_data(
                    data_collection=DataCollection.SENTINEL2_L2A,
                    time_interval=time_interval,
                    maxcc=max_cc
                )
            ],
            responses=[
                SentinelHubRequest.output_response('default', MimeType.TIFF)
            ],
            bbox=bbox,
            size=(512, get_image_dimension(bbox=bbox, width=512)),
            config=config
        )

        preview_request.get_data(save_data=True)
        progress.setValue(3)
        output_file = '/tmp/mosaic_tests' + '/' + preview_request.get_filename_list()[0]
        
        # add file to QGIS
        layer_name = 'default'
        self.iface.addRasterLayer(output_file, layer_name)
        self.iface.messageBar().clearWidgets()

        return None

    def run(self):
        """Run method that loads and starts the plugin"""

        if not self.pluginIsActive:
            self.pluginIsActive = True

            #print "** STARTING SentinelMosaicTester"

            # dockwidget may not exist if:
            #    first run of plugin
            #    removed on close (see self.onClosePlugin method)
            if self.dockwidget == None:
                # Create the dockwidget (after translation) and keep reference
                self.dockwidget = SentinelMosaicTesterDockWidget()

            # connect to provide cleanup on closing of dockwidget
            self.dockwidget.closingPlugin.connect(self.onClosePlugin)

            # show the dockwidget
            # TODO: fix to allow choice of dock location
            self.iface.addDockWidget(Qt.RightDockWidgetArea, self.dockwidget)
            self.dockwidget.show()

            # run different functions depending on custom evalscript or default
            self.dockwidget.order_mosaic_default_btn.clicked.connect(self.run_default)
            self.dockwidget.order_mosaic_custom_evalscript_btn.clicked.connect(self.run_custom_evalscript)
            
                
