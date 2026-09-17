import os.path
import webbrowser

from PyQt5.QtCore import QFileInfo
from PyQt5.QtGui import QIcon
from PyQt5.QtWidgets import QAction, QFileDialog, QInputDialog, QMenu
from qgis.core import (
    QgsCoordinateTransform,
    QgsProject,
    QgsRasterFileWriter,
    QgsRasterLayer,
    QgsRasterPipe,
    QgsSettings,
)

from .config import Config
from .constants import ABOUT_FILE_URL, PLUGIN_NAME, QLR_URL
from .layer_locator_filter import LayerLocatorFilter
from .settings import AusMapOptionsFactory


class AusMap:
    """QGIS Plugin Implementation"""

    def __init__(self, iface):
        """Constructor
        :param iface: An interface instance that will be passed to this class
                      which provides the hook by which you can manipulate the
                      QGIS application at run time.
        :type iface:  QgsInterface
        """
        self.iface = iface  # Reference to the QGIS interface
        self.settings = QgsSettings()

        path = QFileInfo(os.path.realpath(__file__)).path()
        cache_path = path + "/data/"
        if not os.path.exists(cache_path):
            os.makedirs(cache_path)

        self.settings.setValue("cache_path", cache_path)
        self.settings.setValue("ausmap_qlr", QLR_URL)
        self.settings.setValue(
            "help/helpSearchPath",
            [
                "https://docs.qgis.org/$qgis_short_version/$qgis_locale/docs/user_manual/",
                "https://wms-engineering.github.io/AusMap/",
            ],
        )

    def initGui(self):
        self.options_factory = AusMapOptionsFactory(self)
        self.options_factory.setTitle(PLUGIN_NAME)
        self.iface.registerOptionsWidgetFactory(self.options_factory)

        self.create_menu()

    def create_menu(self):
        self.config = Config(self.settings)
        self.config.load()
        self.groups_and_layers = self.config.get_groups_and_layers()

        self.menu = QMenu(self.iface.mainWindow().menuBar())
        self.menu.setObjectName(PLUGIN_NAME)
        self.menu.setTitle(PLUGIN_NAME)

        helper = lambda _id: lambda: self.open_ausmap_node(_id)
        local_helper = lambda _id: lambda: self.open_local_node(_id)

        self.menu_with_actions = []
        layer_action_map = {}  # Used for the locator filter

        for category in self.groups_and_layers:
            for group in category:
                group_menu = QMenu()
                group_menu.setTitle(group["name"])
                for layer in group["selectables"]:
                    action = QAction(layer["name"], self.iface.mainWindow())
                    if layer["source"] == "ausmap":
                        action.triggered.connect(helper(layer["id"]))
                    else:
                        action.triggered.connect(local_helper(layer["id"]))
                    group_menu.addAction(action)

                    layer_action_map[layer["name"]] = action

                self.menu.addMenu(group_menu)
                self.menu_with_actions.append(group_menu)

            self.menu.addSeparator()

        # Add locator filter
        self.layer_locator_filter = LayerLocatorFilter(
            self.iface, layer_action_map
        )
        self.iface.registerLocatorFilter(self.layer_locator_filter)

        # Add About the plugin menu item
        icon_about_path = os.path.join(
            os.path.dirname(__file__), "img/icon_about.png"
        )
        self.export_dem_action = QAction(
            "Export active DEM...",
            self.iface.mainWindow(),
        )
        self.export_dem_action.triggered.connect(self.export_active_dem)

        self.menu.addSeparator()
        self.menu.addAction(self.export_dem_action)

        self.about_menu = QAction(
            QIcon(icon_about_path),
            "About the plugin",
            self.iface.mainWindow(),
        )
        self.about_menu.triggered.connect(self.about_plugin)
        self.menu.addAction(self.about_menu)

        menu_bar = self.iface.mainWindow().menuBar()
        menu_bar.insertMenu(
            self.iface.firstRightStandardMenu().menuAction(), self.menu
        )

    def open_local_node(self, id):
        node = self.config.get_local_maplayer_node(id)
        self.open_node(node, id)

    def open_ausmap_node(self, id):
        node = self.config.get_ausmap_maplayer_node(id)
        self.open_node(node, id)

    def open_node(self, node, id):
        QgsProject.instance().readLayer(node)
        layer = QgsProject.instance().mapLayer(id)
        if layer:
            layer = [
                layer for layer in QgsProject.instance().mapLayers().values()
            ]
            return layer
        else:
            return None

    def export_active_dem(self):
        """Export the active AusMap DEM using another layer's extent."""

        dem = self.iface.activeLayer()

        if not isinstance(dem, QgsRasterLayer):
            self.iface.messageBar().pushWarning(
                PLUGIN_NAME,
                "Select an SRTM DEM layer first.",
            )
            return

        # Native pixel sizes in the DEM's CRS.
        resolutions = {
            "SRTM 1 Sec": 1.0 / 3600.0,
            "SRTM 1 Sec Hydrologically Enforced": 1.0 / 3600.0,
        }

        pixel_size = resolutions.get(dem.name())

        if pixel_size is None:
            self.iface.messageBar().pushWarning(
                PLUGIN_NAME,
                f"No export resolution is configured for {dem.name()}.",
            )
            return

        # Find layers which can supply the export extent.
        extent_layers = [
            layer
            for layer in QgsProject.instance().mapLayers().values()
            if layer.id() != dem.id()
        ]

        if not extent_layers:
            self.iface.messageBar().pushWarning(
                PLUGIN_NAME,
                "No layer is available to supply the export extent.",
            )
            return

        # Include a short ID so duplicate layer names are still distinguishable.
        layer_choices = {
            f"{layer.name()} [{layer.id()[:8]}]": layer
            for layer in extent_layers
        }

        choice, ok = QInputDialog.getItem(
            self.iface.mainWindow(),
            "Export DEM",
            "Use extent from:",
            list(layer_choices.keys()),
            0,
            False,
        )

        if not ok:
            return

        extent_layer = layer_choices[choice]

        output_path, _ = QFileDialog.getSaveFileName(
            self.iface.mainWindow(),
            "Save DEM",
            "",
            "GeoTIFF (*.tif *.tiff)",
        )

        if not output_path:
            return

        if not output_path.lower().endswith((".tif", ".tiff")):
            output_path += ".tif"

        # Get the extent and transform it into the DEM CRS if necessary.
        extent = extent_layer.extent()

        if extent_layer.crs() != dem.crs():
            transform = QgsCoordinateTransform(
                extent_layer.crs(),
                dem.crs(),
                QgsProject.instance(),
            )
            extent = transform.transformBoundingBox(extent)

        # ArcGIS MapServer reports the DEM as 0 x 0, so explicitly calculate
        # the output raster dimensions from the known native resolution.
        columns = max(1, round(extent.width() / pixel_size))
        rows = max(1, round(extent.height() / pixel_size))

        print("AusMap DEM export")
        print("DEM:", dem.name())
        print("Extent layer:", extent_layer.name())
        print("Extent:", extent.toString())
        print("Pixel size:", pixel_size)
        print("Columns:", columns)
        print("Rows:", rows)

        pipe = QgsRasterPipe()

        provider = dem.dataProvider()

        if not pipe.set(provider.clone()):
            self.iface.messageBar().pushCritical(
                PLUGIN_NAME,
                "Could not create raster export pipeline.",
            )
            return

        writer = QgsRasterFileWriter(output_path)
        writer.setOutputFormat("GTiff")
        writer.setCreateOptions(
            [
                "COMPRESS=DEFLATE",
                "TILED=YES",
            ]
        )

        result = writer.writeRaster(
            pipe,
            columns,
            rows,
            extent,
            dem.crs(),
            QgsProject.instance().transformContext(),
        )

        if result == QgsRasterFileWriter.NoError:
            self.iface.messageBar().pushSuccess(
                PLUGIN_NAME,
                f"DEM exported to {output_path}",
            )
        else:
            self.iface.messageBar().pushCritical(
                PLUGIN_NAME,
                f"DEM export failed. Error code: {result}",
            )

    def about_plugin(self):
        webbrowser.open(ABOUT_FILE_URL)

    def unload(self):
        self.iface.unregisterOptionsWidgetFactory(self.options_factory)
        self.iface.deregisterLocatorFilter(self.layer_locator_filter)
        self.options_factory = None
        self.layer_locator_filter = None
        self.clear_menu()

    def reload_menu(self):
        self.clear_menu()
        self.iface.deregisterLocatorFilter(self.layer_locator_filter)
        self.layer_locator_filter = None
        self.create_menu()

    def clear_menu(self):
        # Remove the sub-menus and the menu bar item
        for submenu in self.menu_with_actions:
            if submenu:
                submenu.deleteLater()
        if self.menu:
            self.menu.deleteLater()
        self.menu = None
