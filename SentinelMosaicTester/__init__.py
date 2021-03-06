# -*- coding: utf-8 -*-
"""
/***************************************************************************
 SentinelMosaicTester
                                 A QGIS plugin
 This plugin orders low resolution Sentinel mosaic images
 Generated by Plugin Builder: http://g-sherman.github.io/Qgis-Plugin-Builder/
                             -------------------
        begin                : 2021-03-03
        copyright            : (C) 2021 by SilviaTerra
        email                : henry@silviaterra.com
        git sha              : $Format:%H$
 ***************************************************************************/

/***************************************************************************
 *                                                                         *
 *   This program is free software; you can redistribute it and/or modify  *
 *   it under the terms of the GNU General Public License as published by  *
 *   the Free Software Foundation; either version 2 of the License, or     *
 *   (at your option) any later version.                                   *
 *                                                                         *
 ***************************************************************************/
 This script initializes the plugin, making it known to QGIS.
"""


# noinspection PyPep8Naming
def classFactory(iface):  # pylint: disable=invalid-name
    """Load SentinelMosaicTester class from file SentinelMosaicTester.

    :param iface: A QGIS interface instance.
    :type iface: QgsInterface
    """
    #
    from .sentinel_mosaic_tester import SentinelMosaicTester
    return SentinelMosaicTester(iface)
