import QtQuick 2.15
import QtQuick.Window 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import QtCore 6.10
import "components"

ApplicationWindow {
    id: window
    width: 1200
    height: 720
    minimumWidth: 960
    minimumHeight: 640
    visible: true
    title: "Signal Lab - " + platformTitle
    color: "#02070d"

    FontLoader { id: rajdhaniRegular; source: "fonts/Rajdhani-Regular.ttf" }
    FontLoader { id: rajdhaniSemiBold; source: "fonts/Rajdhani-SemiBold.ttf" }
    FontLoader { id: iconFont; source: "fonts/fa-solid-900.ttf" }

    property color accent: "#12d6ff"
    property color accentAlt: "#f97316"
    property color accentSuccess: "#39e75f"
    property color accentWarn: "#f59e0b"
    property color accentDanger: "#ff4d3d"
    property color accentStripchat: "#ff4d3d"
    property color accentXHamsterLive: "#f97316"
    property color panelColor: "#061521"
    property color panelColorDeep: "#03101a"
    property color panelBorder: "#12394a"
    property color panelBorderStrong: "#1f6b88"
    property color textPrimary: "#eef8ff"
    property color textMuted: "#7f98aa"
    property string headingFont: rajdhaniSemiBold.name !== "" ? rajdhaniSemiBold.name : "Bahnschrift"
    property string bodyFont: rajdhaniRegular.name !== "" ? rajdhaniRegular.name : "Bahnschrift"
    property string monoFont: "Cascadia Code"
    readonly property string iconFontFamily: iconFont.name
    readonly property bool manualVpn: appController.settings.vpnProvider !== "mullvad"
    property string selectedModel: ""
    property string selectedMasterModel: ""
    property var countries: appController.countryChoices
    readonly property bool camGirlFinderEnabled: {
        var rows = appController.searchProviderRows || []
        for (var i = 0; i < rows.length; i++) {
            if (rows[i].id === "camgirlfinder") {
                return rows[i].enabled
            }
        }
        return false
    }
    property alias modelSearchText: modelSearchInput.text
    property int filteredModelCount: 0
    property var pinnedModels: []
    property var pinnedVisibleModels: []
    property var unpinnedModels: []
    property int pinnedSectionMaxHeight: 160
    property int openBatchIndex: 0
    property int openBatchSize: appController.settings.openBatchSize
    property bool layoutDirty: false
    property bool layoutRestoring: true

    property int leftPreferredWidth: 420
    property int rightPreferredWidth: 680
    property int sessionPreferredHeight: 200
    property int stepsPreferredHeight: 520
    property int activityPreferredHeight: 480
    property int blockedPreferredHeight: 220
    property bool isMfc: appController.platform === "MyFreeCams"
    property bool isStripchat: appController.platform === "Stripchat"
    property bool isXHamsterLive: appController.platform === "XHamsterLive"
    property string platformTitle: isMfc
        ? "MFC Scraper"
        : isStripchat
            ? "Stripchat Scraper"
            : isXHamsterLive
                ? "XHamsterLive Scraper"
                : "CTB Scrape V3"
    property string platformSubtitle: isMfc
        ? "MyFreeCams session control and comparison"
        : isStripchat
            ? "Stripchat session control and verification"
            : isXHamsterLive
                ? "XHamsterLive session control and verification"
                : "Chaturbate session control and verification"
    property string blockedTitle: isMfc ? "Live Models" : "Blocked Models"
    property string step4HelpText: isMfc
        ? "Step 4: Verify live video playback (MyFreeCams)."
        : isStripchat
            ? "Step 4: Verify hidden accounts (Stripchat)."
            : isXHamsterLive
                ? "Step 4: Verify hidden accounts (XHamsterLive)."
                : "Step 4: Verify candidates (Chaturbate only)."
    property string step4ButtonText: isMfc
        ? "Run Step 4 - Verify Live"
        : isStripchat
            ? "Run Step 4 - Verify Hidden"
            : isXHamsterLive
                ? "Run Step 4 - Verify Hidden"
                : "Run Step 4 - Verify"

    Connections {
        target: appController
        function onMasterListCompiled(addedCount, totalCount) {
            sessionSummaryPopup.newModelsCount = addedCount
            sessionSummaryPopup.totalModelsCount = totalCount
            sessionSummaryPopup.open()
        }
    }

    SearchProviderMenu {
        id: searchProviderMenu
        popupHost: window.contentItem
        providers: appController.externalSearchProviders
        headingFont: window.headingFont
        bodyFont: window.bodyFont
        iconFont: iconFont.name
        accent: window.accent
        panelColor: window.panelColor
        panelColorDeep: window.panelColorDeep
        panelBorder: window.panelBorder
        panelBorderStrong: window.panelBorderStrong
        textPrimary: window.textPrimary
        textMuted: window.textMuted

        onProviderSelected: function(providerId, modelName) {
            appController.openExternalSearch(providerId, modelName)
        }
        onAllProvidersSelected: function(modelName) {
            appController.openAllExternalSearches(modelName)
        }
    }

    Settings {
        id: layoutSettings
        category: "layout"
        property int leftWidth: 420
        property int rightWidth: 680
        property int sessionHeight: 200
        property int stepsHeight: 520
        property int activityHeight: 480
        property int blockedHeight: 220
    }

    Settings {
        id: modelSettings
        category: "models"
        property string pinnedModelsJson: "[]"
    }

    Settings {
        id: windowSettings
        category: "window"
        property int windowX: -1
        property int windowY: -1
        property int windowWidth: 1200
        property int windowHeight: 720
    }

    function markLayoutDirty() {
        if (!layoutRestoring) {
            layoutDirty = true
        }
    }

    function applySavedLayout() {
        layoutRestoring = true
        leftPreferredWidth = Math.max(320, layoutSettings.leftWidth)
        rightPreferredWidth = Math.max(360, layoutSettings.rightWidth)
        sessionPreferredHeight = Math.max(160, layoutSettings.sessionHeight)
        stepsPreferredHeight = Math.max(260, layoutSettings.stepsHeight)
        activityPreferredHeight = Math.max(220, layoutSettings.activityHeight)
        blockedPreferredHeight = Math.max(160, layoutSettings.blockedHeight)
        layoutDirty = false
        Qt.callLater(function() { layoutRestoring = false })
    }

    function saveLayout() {
        layoutSettings.leftWidth = Math.round(leftSplit.width)
        layoutSettings.rightWidth = Math.round(rightSplit.width)
        layoutSettings.sessionHeight = Math.round(sessionPanel.height)
        layoutSettings.stepsHeight = Math.round(stepsPanel.height)
        layoutSettings.activityHeight = Math.round(activityPanel.height)
        layoutSettings.blockedHeight = Math.round(blockedPanel.height)
        layoutDirty = false
    }

    function resetLayout() {
        layoutSettings.leftWidth = 420
        layoutSettings.rightWidth = 680
        layoutSettings.sessionHeight = 200
        layoutSettings.stepsHeight = 520
        layoutSettings.activityHeight = 480
        layoutSettings.blockedHeight = 220
        applySavedLayout()
        leftSplit.SplitView.preferredWidth = 420
        rightSplit.SplitView.preferredWidth = 680
        sessionPanel.SplitView.preferredHeight = 200
        stepsPanel.SplitView.preferredHeight = 520
        activityPanel.SplitView.preferredHeight = 480
        blockedPanel.SplitView.preferredHeight = 220
    }

    function clearPinnedModels() {
        pinnedModels = []
        savePinnedModels()
        rebuildModelLists()
    }

    function applySavedGeometry() {
        if (!appController.settings.rememberWindowGeometry) {
            return
        }
        var savedWidth = Math.max(minimumWidth, windowSettings.windowWidth)
        var savedHeight = Math.max(minimumHeight, windowSettings.windowHeight)
        width = Math.min(savedWidth, Screen.desktopAvailableWidth)
        height = Math.min(savedHeight, Screen.desktopAvailableHeight)
        var savedX = windowSettings.windowX
        var savedY = windowSettings.windowY
        if (savedX < 0 || savedY < 0) {
            return
        }
        if (savedX + width > Screen.desktopAvailableWidth
                || savedY + height > Screen.desktopAvailableHeight) {
            return
        }
        x = savedX
        y = savedY
    }

    function saveGeometry() {
        if (!appController.settings.rememberWindowGeometry
                || visibility === Window.Minimized) {
            return
        }
        if (visibility === Window.Windowed) {
            windowSettings.windowX = Math.round(x)
            windowSettings.windowY = Math.round(y)
        }
        windowSettings.windowWidth = Math.round(width)
        windowSettings.windowHeight = Math.round(height)
    }

    function arraysEqual(a, b) {
        if (!a || !b) {
            return false
        }
        if (a.length !== b.length) {
            return false
        }
        for (var i = 0; i < a.length; i++) {
            if (a[i] !== b[i]) {
                return false
            }
        }
        return true
    }

    function normalizeSearchText(value) {
        return String(value || "").trim().toLowerCase()
    }

    function modelMatchesSearch(modelText, query) {
        if (!query || query.length === 0) {
            return true
        }
        var raw = String(modelText || "")
        if (raw.length === 0) {
            return false
        }
        var lowered = raw.toLowerCase()
        if (lowered.indexOf(query) !== -1) {
            return true
        }
        var trimmed = raw
        if (trimmed.length > 0 && trimmed.charAt(trimmed.length - 1) === "/") {
            trimmed = trimmed.slice(0, -1)
        }
        var parts = trimmed.split("/")
        var last = parts.length ? parts[parts.length - 1] : trimmed
        if (last.indexOf("#") === 0) {
            last = last.slice(1)
        }
        return last.toLowerCase().indexOf(query) !== -1
    }

    function normalizePinnedModels(list) {
        var cleaned = []
        var seen = {}
        if (!list) {
            return cleaned
        }
        for (var i = 0; i < list.length; i++) {
            var value = list[i]
            if (value === null || value === undefined) {
                continue
            }
            var item = String(value).trim()
            if (item.length === 0 || seen[item]) {
                continue
            }
            seen[item] = true
            cleaned.push(item)
        }
        return cleaned
    }

    function loadPinnedModels() {
        var parsed = []
        try {
            parsed = JSON.parse(modelSettings.pinnedModelsJson || "[]")
        } catch (err) {
            parsed = []
        }
        if (!parsed || parsed.length === undefined) {
            parsed = []
        }
        var cleaned = normalizePinnedModels(parsed)
        if (!arraysEqual(cleaned, pinnedModels)) {
            pinnedModels = cleaned
            savePinnedModels()
        }
    }

    function savePinnedModels() {
        modelSettings.pinnedModelsJson = JSON.stringify(pinnedModels)
    }

    function rebuildModelLists() {
        var blocked = appController.blockedModels || []
        var blockedLookup = {}
        for (var i = 0; i < blocked.length; i++) {
            blockedLookup[blocked[i]] = true
        }

        var searchQuery = normalizeSearchText(modelSearchText)
        var pinnedLookup = {}
        var visiblePinned = []
        for (var j = 0; j < pinnedModels.length; j++) {
            var pinnedItem = pinnedModels[j]
            if (blockedLookup[pinnedItem] && !pinnedLookup[pinnedItem]) {
                pinnedLookup[pinnedItem] = true
                if (modelMatchesSearch(pinnedItem, searchQuery)) {
                    visiblePinned.push(pinnedItem)
                }
            }
        }

        var remaining = []
        for (var k = 0; k < blocked.length; k++) {
            var model = blocked[k]
            if (!pinnedLookup[model] && modelMatchesSearch(model, searchQuery)) {
                remaining.push(model)
            }
        }

        pinnedVisibleModels = visiblePinned
        unpinnedModels = remaining
        filteredModelCount = pinnedVisibleModels.length + unpinnedModels.length

        var total = pinnedVisibleModels.length + unpinnedModels.length
        if (openBatchIndex >= total) {
            openBatchIndex = 0
        }

        if (selectedModel.length > 0 && !blockedLookup[selectedModel]) {
            selectedModel = ""
        }
    }

    function togglePinnedModel(modelText) {
        var trimmed = modelText.trim()
        if (trimmed.length === 0) {
            return
        }
        var nextPinned = pinnedModels.slice(0)
        var index = nextPinned.indexOf(trimmed)
        if (index === -1) {
            nextPinned.push(trimmed)
        } else {
            nextPinned.splice(index, 1)
        }
        pinnedModels = nextPinned
        savePinnedModels()
        rebuildModelLists()
    }

    function modelUrlFromText(rawText) {
        var t = String(rawText || "").trim()
        if (t.length === 0) {
            return ""
        }
        if (t.indexOf("#") === 0) {
            t = t.slice(1)
        }
        if (t.indexOf("://") !== -1) {
            return t
        }
        if (t.indexOf("www.") === 0) {
            return "https://" + t
        }
        if (t.indexOf("chaturbate.com/") === 0) {
            return "https://" + t
        }
        if (t.indexOf("myfreecams.com/") === 0 || t.indexOf("myfreecams.com") === 0) {
            return "https://" + t
        }
        if (t.indexOf("stripchat.com/") === 0) {
            return "https://" + t
        }
        if (t.indexOf("xhamsterlive.com/") === 0) {
            return "https://" + t
        }
        if (window.isMfc) {
            return "https://www.myfreecams.com/#" + t
        }
        if (window.isStripchat) {
            return "https://stripchat.com/" + t
        }
        if (window.isXHamsterLive) {
            return "https://xhamsterlive.com/" + t
        }
        return "https://chaturbate.com/" + t
    }

    function openNextBatch() {
        var combined = []
        for (var i = 0; i < pinnedVisibleModels.length; i++) {
            combined.push(pinnedVisibleModels[i])
        }
        for (var j = 0; j < unpinnedModels.length; j++) {
            combined.push(unpinnedModels[j])
        }
        var total = combined.length
        if (total === 0) {
            return
        }
        if (openBatchIndex >= total || openBatchIndex < 0) {
            openBatchIndex = 0
        }
        var end = Math.min(openBatchIndex + openBatchSize, total)
        var urls = []
        for (var k = openBatchIndex; k < end; k++) {
            var url = modelUrlFromText(combined[k])
            if (url.length > 0) {
                urls.push(url)
            }
        }
        if (urls.length > 0) {
            appController.openModelUrls(urls)
        }
        openBatchIndex = end
        if (openBatchIndex >= total) {
            openBatchIndex = 0
        }
    }

    onModelSearchTextChanged: rebuildModelLists()

    Component.onCompleted: {
        loadPinnedModels()
        rebuildModelLists()
        applySavedGeometry()
        Qt.callLater(function() { applySavedLayout() })
    }

    onClosing: window.saveGeometry()

    Image {
        anchors.fill: parent
        source: "assets/generated/signal-lab-background.png"
        fillMode: Image.PreserveAspectCrop
        opacity: 0.72
        cache: true
        asynchronous: true
    }

    Rectangle {
        anchors.fill: parent
        gradient: Gradient {
            GradientStop { position: 0.0; color: "#02070d" }
            GradientStop { position: 0.45; color: "#8802070d" }
            GradientStop { position: 1.0; color: "#02070d" }
        }
    }

    Item {
        anchors.fill: parent
        opacity: 0.045
        Repeater {
            model: Math.floor(parent.width / 140)
            Rectangle {
                x: index * 140
                y: 0
                width: 1
                height: parent.height
                color: window.accent
            }
        }
        Repeater {
            model: Math.floor(parent.height / 140)
            Rectangle {
                x: 0
                y: index * 140
                width: parent.width
                height: 1
                color: window.panelBorderStrong
            }
        }
    }

    Rectangle {
        height: 1
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        gradient: Gradient {
            orientation: Gradient.Horizontal
            GradientStop { position: 0.0; color: "transparent" }
            GradientStop { position: 0.25; color: window.accent }
            GradientStop { position: 0.75; color: window.accent }
            GradientStop { position: 1.0; color: "transparent" }
        }
        opacity: 0.4
    }

    Component {
        id: blockedModelDelegate

        Item {
            id: blockedRow
            width: ListView.view.width - (ListView.view.scrollGutter || 0)
            height: 44
            property bool pinned: ListView.view && ListView.view.pinnedList
            property string rawText: modelData
            property string handleText: {
                var t = rawText
                if (t.length > 0 && t.charAt(t.length - 1) === "/") {
                    t = t.slice(0, -1)
                }
                var parts = t.split("/")
                var last = parts.length ? parts[parts.length - 1] : t
                if (last.indexOf("#") === 0) {
                    last = last.slice(1)
                }
                return last
            }
            property string modelUrl: window.modelUrlFromText(rawText)
            property bool hovered: false
            property bool selected: window.selectedModel === rawText

            Rectangle {
                anchors.fill: parent
                radius: 6
                color: blockedRow.selected
                    ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.13)
                    : (blockedRow.hovered ? Qt.rgba(1, 1, 1, 0.045) : (index % 2 === 0 ? "transparent" : Qt.rgba(1, 1, 1, 0.02)))
                border.color: blockedRow.selected
                    ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.65)
                    : "transparent"
                border.width: blockedRow.selected ? 1 : 0
                Behavior on color { ColorAnimation { duration: 100 } }
            }

            Column {
                anchors.fill: parent
                anchors.margins: 6
                anchors.rightMargin: 150
                spacing: 2

                Text {
                    text: handleText
                    font.family: window.headingFont
                    font.pixelSize: 13
                    font.letterSpacing: 0
                    color: window.textPrimary
                }
                Text {
                    width: parent.width
                    text: rawText
                    font.family: window.monoFont
                    font.pixelSize: 10
                    font.letterSpacing: 0
                    color: window.textMuted
                    elide: Text.ElideRight
                }
            }

            Rectangle {
                id: externalSearchBadge
                anchors.right: camGirlFinderBadge.left
                anchors.rightMargin: camGirlFinderBadge.visible ? 6 : 0
                anchors.verticalCenter: parent.verticalCenter
                width: 28
                height: 22
                radius: 6
                color: externalSearchArea.containsMouse ? "#0b3445" : window.panelColorDeep
                border.color: externalSearchArea.containsMouse ? window.accent : Qt.rgba(1, 1, 1, 0.08)
                border.width: 1
                z: 2

                Text {
                    anchors.centerIn: parent
                    text: "\uf002"
                    font.family: iconFont.name
                    font.pixelSize: 10
                    color: externalSearchArea.containsMouse ? window.accent : window.textMuted
                }

                ToolTip.visible: externalSearchArea.containsMouse
                ToolTip.text: "Choose where to search " + blockedRow.handleText
                ToolTip.delay: 350
            }

            MouseArea {
                id: externalSearchArea
                anchors.fill: externalSearchBadge
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                z: 3
                onClicked: function(mouse) {
                    mouse.accepted = true
                    window.selectedModel = blockedRow.rawText
                    searchProviderMenu.openFor(
                        externalSearchBadge,
                        blockedRow.handleText
                    )
                }
            }

            Rectangle {
                id: camGirlFinderBadge
                anchors.right: pinBadge.left
                anchors.rightMargin: visible ? 6 : 0
                anchors.verticalCenter: parent.verticalCenter
                visible: window.camGirlFinderEnabled
                width: visible ? 38 : 0
                height: 22
                radius: 6
                color: camGirlFinderArea.containsMouse ? "#421b2a" : window.panelColorDeep
                border.color: camGirlFinderArea.containsMouse ? "#f05b78" : Qt.rgba(1, 1, 1, 0.08)
                border.width: 1
                z: 2

                Text {
                    anchors.centerIn: parent
                    text: "CGF"
                    font.family: window.headingFont
                    font.pixelSize: 9
                    font.bold: true
                    color: camGirlFinderArea.containsMouse ? "#ff8399" : window.textMuted
                }

                ToolTip.visible: camGirlFinderArea.containsMouse
                ToolTip.text: "Search " + blockedRow.handleText + " on CamGirlFinder"
                ToolTip.delay: 350
            }

            MouseArea {
                id: camGirlFinderArea
                anchors.fill: camGirlFinderBadge
                hoverEnabled: true
                enabled: camGirlFinderBadge.visible
                cursorShape: Qt.PointingHandCursor
                z: 3
                onClicked: function(mouse) {
                    mouse.accepted = true
                    window.selectedModel = blockedRow.rawText
                    appController.openExternalSearch("camgirlfinder", blockedRow.handleText)
                }
            }

            Rectangle {
                id: pinBadge
                anchors.right: parent.right
                anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                height: 22
                radius: 6
                color: blockedRow.pinned
                    ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.16)
                    : window.panelColorDeep
                border.color: blockedRow.pinned
                    ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.7)
                    : Qt.rgba(1, 1, 1, 0.08)
                border.width: 1
                z: 2
                width: pinText.implicitWidth + 16

                Text {
                    id: pinText
                    anchors.centerIn: parent
                    text: blockedRow.pinned ? "UNPIN" : "PIN"
                    font.family: window.headingFont
                    font.pixelSize: 9
                    font.letterSpacing: 0.8
                    color: blockedRow.pinned ? window.accent : window.textMuted
                }
            }

            MouseArea {
                anchors.fill: pinBadge
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                z: 3
                onClicked: function(mouse) {
                    mouse.accepted = true
                    window.togglePinnedModel(blockedRow.rawText)
                }
            }

            MouseArea {
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: blockedRow.modelUrl.length > 0 ? Qt.PointingHandCursor : Qt.ArrowCursor
                z: 1
                onEntered: blockedRow.hovered = true
                onExited: blockedRow.hovered = false
                onClicked: {
                    window.selectedModel = blockedRow.rawText
                    if (blockedRow.modelUrl.length > 0) {
                        appController.copyModelReference(
                            blockedRow.handleText,
                            blockedRow.modelUrl
                        )
                        appController.openModelUrl(blockedRow.modelUrl)
                    }
                }
            }
        }
    }

    ColumnLayout {
        id: mainLayout
        anchors.fill: parent
        anchors.margins: 16
        spacing: 0

        StackLayout {
            id: mainStack
            currentIndex: 0
            Layout.fillWidth: true
            Layout.fillHeight: true

            // --- INDEX 0: SCRAPER ---
            ColumnLayout {
                id: scraperView
                Layout.fillWidth: true
                Layout.fillHeight: true
                Layout.margins: 24
                spacing: 16

                Component.onCompleted: {
                    scraperView.opacity = 1.0
                    scraperView.scale = 1.0
                }



        RowLayout {
            Layout.fillWidth: true
            spacing: 12

            RowLayout {
                Layout.preferredWidth: 300
                Layout.minimumWidth: 240
                spacing: 12

                Image {
                    Layout.preferredWidth: 40
                    Layout.preferredHeight: 40
                    source: "assets/generated/app-icon.png"
                    fillMode: Image.PreserveAspectFit
                    asynchronous: true
                    cache: true
                    visible: status === Image.Ready
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 0

                    Text {
                        text: "SIGNAL LAB"
                        font.family: window.headingFont
                        font.pixelSize: 26
                        font.letterSpacing: 2.5
                        color: window.textPrimary
                    }
                    Text {
                        text: window.platformSubtitle
                        font.family: window.bodyFont
                        font.pixelSize: 12
                        font.letterSpacing: 0.3
                        color: window.textMuted
                        elide: Text.ElideRight
                        Layout.fillWidth: true
                    }
                }
            }

            SignalStatCard {
                label: "SYSTEM STATUS"
                value: appController.busy ? "RUNNING" : "STANDBY"
                hint: appController.status
                icon: appController.busy ? "\uf04b" : "\uf111"
                iconFontFamily: iconFont.name
                accent: appController.busy ? window.accentAlt : window.accentSuccess
                surface: window.panelColor
                borderColor: window.panelBorder
                textPrimary: window.textPrimary
                textMuted: window.textMuted
                headingFont: window.headingFont
                bodyFont: window.bodyFont
                Layout.fillWidth: true
                Layout.preferredWidth: 190
            }

            SignalStatCard {
                label: "ACTIVE PLATFORM"
                value: appController.platform
                hint: window.blockedTitle
                icon: "\uf1de"
                iconFontFamily: iconFont.name
                accent: window.isStripchat ? window.accentStripchat : (window.isXHamsterLive ? window.accentXHamsterLive : window.accent)
                surface: window.panelColor
                borderColor: window.panelBorder
                textPrimary: window.textPrimary
                textMuted: window.textMuted
                headingFont: window.headingFont
                bodyFont: window.bodyFont
                Layout.fillWidth: true
                Layout.preferredWidth: 190
            }

            SignalStatCard {
                label: "LOADED TARGETS"
                value: appController.blockedModels.length + " items"
                hint: window.modelSearchText.trim().length > 0 ? window.filteredModelCount + " visible" : "ready for review"
                icon: "\uf05e"
                iconFontFamily: iconFont.name
                accent: window.accentAlt
                surface: window.panelColor
                borderColor: window.panelBorder
                textPrimary: window.textPrimary
                textMuted: window.textMuted
                headingFont: window.headingFont
                bodyFont: window.bodyFont
                Layout.fillWidth: true
                Layout.preferredWidth: 180
            }

            AppButton {
                text: window.layoutDirty ? "Save Layout" : "Layout Saved"
                icon: window.layoutDirty ? "\uf0c7" : "\uf00c"
                iconFontFamily: iconFont.name
                kind: window.layoutDirty ? "secondary" : "ghost"
                accent: window.layoutDirty ? window.accent : window.panelBorderStrong
                fontFamily: window.headingFont
                enabled: window.layoutDirty
                compact: true
                Layout.preferredWidth: 132
                Layout.preferredHeight: 34
                Layout.alignment: Qt.AlignVCenter
                onClicked: window.saveLayout()
            }
        }

        SplitView {
            id: mainSplit
            Layout.fillWidth: true
            Layout.fillHeight: true
            orientation: Qt.Horizontal
            handle: Item {
                id: mainHandle
                implicitWidth: 9
                implicitHeight: 9
                Rectangle {
                    anchors.centerIn: parent
                    width: 2
                    height: 44
                    radius: 1
                    color: window.accent
                    opacity: mainHandle.SplitHandle.pressed ? 1.0
                        : (mainHandle.SplitHandle.hovered ? 0.75 : 0.18)
                    Behavior on opacity { NumberAnimation { duration: 120 } }
                }
            }

            Item {
                id: signalRail
                SplitView.preferredWidth: 224
                SplitView.minimumWidth: 196
                SplitView.maximumWidth: 260

                Rectangle {
                    anchors.fill: parent
                    radius: 8
                    gradient: Gradient {
                        GradientStop { position: 0.0; color: "#04111d" }
                        GradientStop { position: 1.0; color: "#020910" }
                    }
                    border.color: window.panelBorder
                    border.width: 1
                }

                Image {
                    anchors.left: parent.left
                    anchors.right: parent.right
                    anchors.bottom: parent.bottom
                    anchors.bottomMargin: 8
                    height: Math.min(parent.height * 0.42, 280)
                    source: "assets/generated/signal-lab-sidebar.png"
                    fillMode: Image.PreserveAspectCrop
                    opacity: 0.42
                    cache: true
                    asynchronous: true
                    clip: true
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 14
                    spacing: 12

                    ColumnLayout {
                        Layout.fillWidth: true
                        spacing: 2

                        Text {
                            text: "CONTROL CENTER"
                            font.family: window.headingFont
                            font.pixelSize: 13
                            color: window.accent
                        }
                        Text {
                            text: window.platformTitle
                            font.family: window.bodyFont
                            font.pixelSize: 11
                            color: window.textMuted
                            elide: Text.ElideRight
                            Layout.fillWidth: true
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        height: 1
                        color: window.panelBorder
                    }

                    SignalRailButton {
                        text: "Dashboard"
                        icon: "\uf3fd"
                        iconFontFamily: iconFont.name
                        active: mainStack.currentIndex === 0
                        accent: window.accent
                        textPrimary: window.textPrimary
                        textMuted: window.textMuted
                        fontFamily: window.headingFont
                        Layout.fillWidth: true
                        onClicked: mainStack.currentIndex = 0
                    }

                    SignalRailButton {
                        text: "Master List"
                        icon: "\uf022"
                        iconFontFamily: iconFont.name
                        active: mainStack.currentIndex === 1
                        accent: window.accent
                        textPrimary: window.textPrimary
                        textMuted: window.textMuted
                        fontFamily: window.headingFont
                        Layout.fillWidth: true
                        enabled: !appController.busy
                        onClicked: mainStack.currentIndex = 1
                    }

                    SignalRailButton {
                        text: "Preferences"
                        icon: "\uf013"
                        iconFontFamily: iconFont.name
                        active: mainStack.currentIndex === 2
                        accent: window.accent
                        textPrimary: window.textPrimary
                        textMuted: window.textMuted
                        fontFamily: window.headingFont
                        Layout.fillWidth: true
                        onClicked: mainStack.currentIndex = 2
                    }

                    Text {
                        text: "PLATFORM SELECTOR"
                        font.family: window.headingFont
                        font.pixelSize: 11
                        color: window.textMuted
                        Layout.topMargin: 10
                    }

                    SignalRailButton {
                        text: "Chaturbate"
                        icon: "\uf0ac"
                        iconFontFamily: iconFont.name
                        active: !window.isMfc && !window.isStripchat && !window.isXHamsterLive
                        accent: window.accent
                        textPrimary: window.textPrimary
                        textMuted: window.textMuted
                        fontFamily: window.headingFont
                        Layout.fillWidth: true
                        enabled: !appController.busy
                        onClicked: appController.setPlatform("Chaturbate")
                    }

                    SignalRailButton {
                        text: "MyFreeCams"
                        icon: "\uf007"
                        iconFontFamily: iconFont.name
                        active: window.isMfc
                        accent: window.accentAlt
                        textPrimary: window.textPrimary
                        textMuted: window.textMuted
                        fontFamily: window.headingFont
                        Layout.fillWidth: true
                        enabled: !appController.busy
                        onClicked: appController.setPlatform("MyFreeCams")
                    }

                    SignalRailButton {
                        text: "Stripchat"
                        icon: "\uf06e"
                        iconFontFamily: iconFont.name
                        active: window.isStripchat
                        accent: window.accentStripchat
                        textPrimary: window.textPrimary
                        textMuted: window.textMuted
                        fontFamily: window.headingFont
                        Layout.fillWidth: true
                        enabled: !appController.busy
                        onClicked: appController.setPlatform("Stripchat")
                    }

                    SignalRailButton {
                        text: "XHamsterLive"
                        icon: "\uf1e6"
                        iconFontFamily: iconFont.name
                        active: window.isXHamsterLive
                        accent: window.accentXHamsterLive
                        textPrimary: window.textPrimary
                        textMuted: window.textMuted
                        fontFamily: window.headingFont
                        Layout.fillWidth: true
                        enabled: !appController.busy
                        onClicked: appController.setPlatform("XHamsterLive")
                    }

                    Item { Layout.fillHeight: true }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 86
                        radius: 6
                        color: "#020910"
                        border.color: window.panelBorder
                        border.width: 1

                        Column {
                            anchors.fill: parent
                            anchors.margins: 10
                            spacing: 6

                            Text {
                                text: "ENGINE"
                                font.family: window.headingFont
                                font.pixelSize: 10
                                color: window.textMuted
                            }
                            Text {
                                text: appController.busy ? "Task active" : "Ready for next run"
                                font.family: window.bodyFont
                                font.pixelSize: 11
                                color: appController.busy ? window.accentAlt : window.accentSuccess
                                elide: Text.ElideRight
                                width: parent.width
                            }
                            Text {
                                text: appController.sessionPath.length > 0 ? "Session linked" : "No session loaded"
                                font.family: window.bodyFont
                                font.pixelSize: 11
                                color: window.textMuted
                                elide: Text.ElideRight
                                width: parent.width
                            }
                        }
                    }
                }
            }

            SplitView {
                id: leftSplit
                orientation: Qt.Vertical
                SplitView.preferredWidth: window.leftPreferredWidth
                SplitView.minimumWidth: 320
                onWidthChanged: window.markLayoutDirty()
                handle: Item {
                    id: leftHandle
                    implicitWidth: 9
                    implicitHeight: 9
                    Rectangle {
                        anchors.centerIn: parent
                        width: 44
                        height: 2
                        radius: 1
                        color: window.accent
                        opacity: leftHandle.SplitHandle.pressed ? 1.0
                            : (leftHandle.SplitHandle.hovered ? 0.75 : 0.18)
                        Behavior on opacity { NumberAnimation { duration: 120 } }
                    }
                }

                Panel {
                    id: sessionPanel
                    SplitView.preferredHeight: window.sessionPreferredHeight
                    SplitView.minimumHeight: 160
                    panelColor: window.panelColor
                    borderColor: window.panelBorder
                    accentColor: window.accent
                    title: "Session"
                    titleIcon: "\uf07b"
                    titleIconFont: iconFont.name
                    titleFont: window.headingFont
                    titleColor: window.textPrimary
                    onHeightChanged: window.markLayoutDirty()

                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 10

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: sessionPathText.implicitHeight + 14
                            radius: 6
                            color: window.panelColorDeep
                            border.color: Qt.rgba(1, 1, 1, 0.05)
                            border.width: 1

                            Text {
                                id: sessionPathText
                                anchors.fill: parent
                                anchors.margins: 7
                                anchors.leftMargin: 10
                                anchors.rightMargin: 10
                                text: appController.sessionPath.length > 0 ? appController.sessionPath : "No session selected"
                                font.family: window.monoFont
                                font.pixelSize: 10
                                color: appController.sessionPath.length > 0 ? "#a9c4d6" : window.textMuted
                                wrapMode: Text.WrapAnywhere
                                verticalAlignment: Text.AlignVCenter
                            }
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 10

                            AppButton {
                                text: "Create New"
                                icon: "\uf067"
                                iconFontFamily: iconFont.name
                                accent: window.accent
                                fontFamily: window.headingFont
                                enabled: !appController.busy
                                Layout.fillWidth: true
                                onClicked: appController.createSession()
                            }
                            AppButton {
                                text: "Use Latest"
                                icon: "\uf1da"
                                iconFontFamily: iconFont.name
                                accent: window.accent
                                fontFamily: window.headingFont
                                enabled: !appController.busy
                                Layout.fillWidth: true
                                onClicked: appController.useLatestSession()
                            }
                        }

                        AppButton {
                            text: "Open Session Folder"
                            icon: "\uf07c"
                            iconFontFamily: iconFont.name
                            kind: "ghost"
                            accent: window.accent
                            fontFamily: window.headingFont
                            enabled: appController.sessionPath.length > 0
                            Layout.fillWidth: true
                            onClicked: appController.openSessionFolder()
                        }
                    }
                }

                Panel {
                    id: stepsPanel
                    SplitView.preferredHeight: window.stepsPreferredHeight
                    SplitView.minimumHeight: 260
                    panelColor: window.panelColor
                    borderColor: window.panelBorder
                    accentColor: window.accent
                    title: "Workflow Control"
                    titleIcon: "\uf085"
                    titleIconFont: iconFont.name
                    titleFont: window.headingFont
                    titleColor: window.textPrimary
                    onHeightChanged: window.markLayoutDirty()

                    ScrollView {
                        id: stepsScroll
                        anchors.fill: parent
                        clip: true
                        contentWidth: availableWidth
                        ScrollBar.horizontal.policy: ScrollBar.AlwaysOff
                        // The bar lives in the panel's right padding, fully
                        // outside the content column, so it can never overlap.
                        ScrollBar.vertical: AppScrollBar {
                            id: stepsScrollBar
                            accent: window.accent
                            parent: stepsScroll
                            x: stepsScroll.width + 3
                            height: stepsScroll.availableHeight
                        }

                    ColumnLayout {
                        width: stepsScroll.availableWidth
                        spacing: 12

                        AppToggle {
                            id: hideBrowserToggle
                            text: "Hide browser (steps 1-2)"
                            checked: appController.settings.hideBrowserSteps12
                            accent: window.accent
                            fontFamily: window.bodyFont
                            Layout.fillWidth: true
                            visible: !window.isMfc && !window.isStripchat
                            onToggled: appController.setSetting(
                                "hideBrowserSteps12", checked
                            )

                            Connections {
                                target: appController
                                function onSettingsChanged() {
                                    hideBrowserToggle.checked =
                                        appController.settings.hideBrowserSteps12
                                }
                            }
                        }

                        AppToggle {
                            id: minimizeToggle
                            text: "Hide browser except captcha (step 4)"
                            checked: appController.settings.hideBrowserStep4
                            accent: window.accent
                            fontFamily: window.bodyFont
                            Layout.fillWidth: true
                            visible: !window.isMfc
                            onToggled: appController.setSetting(
                                "hideBrowserStep4", checked
                            )

                            Connections {
                                target: appController
                                function onSettingsChanged() {
                                    minimizeToggle.checked =
                                        appController.settings.hideBrowserStep4
                                }
                            }
                        }

                        AppButton {
                            objectName: "fullAutoButton"
                            text: "Run Full Auto Flow"
                            icon: "\uf04b"
                            iconFontFamily: iconFont.name
                            kind: "primary"
                            accent: window.accent
                            fontFamily: window.headingFont
                            enabled: !appController.busy && !window.manualVpn
                            Layout.fillWidth: true
                            Layout.topMargin: 4
                            onClicked: appController.startFullAutoFlow(
                                window.isStripchat ? false : hideBrowserToggle.checked,
                                minimizeToggle.checked
                            )
                        }

                        Text {
                            text: window.manualVpn
                                ? "Manual / any VPN: control your VPN yourself. Full Auto is unavailable. "
                                  + "Prepare the app and browser network, then click a step to confirm it is ready."
                                : "Mullvad (automatic): Full Auto connects the selected relay for Step 1 "
                                  + "and disconnects for Steps 2 and 4. Step 3 uses saved lists only."
                            font.family: window.bodyFont
                            font.pixelSize: 11
                            color: window.textMuted
                            opacity: 0.85
                            wrapMode: Text.WordWrap
                            Layout.fillWidth: true
                        }

                        AppButton {
                            objectName: "resumeFullAutoButton"
                            text: "Resume Safe Run"
                            icon: "\uf021"
                            iconFontFamily: iconFont.name
                            accent: "#f6c85f"
                            fontFamily: window.headingFont
                            visible: appController.resumable
                            enabled: !appController.busy && !window.manualVpn
                            Layout.fillWidth: true
                            onClicked: appController.resumeFullAutoFlow()
                        }

                        Rectangle {
                            Layout.fillWidth: true
                            Layout.preferredHeight: runStatusText.implicitHeight + 14
                            visible: appController.busy
                                || Object.keys(appController.lastOutcome).length > 0
                            radius: 6
                            color: window.panelColorDeep
                            border.width: 1
                            border.color: appController.busy
                                ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.4)
                                : (appController.lastOutcome.status === "failed"
                                    ? "#66ff6b61" : Qt.rgba(1, 1, 1, 0.06))

                            Rectangle {
                                id: busyDot
                                width: 6
                                height: 6
                                radius: 3
                                anchors.left: parent.left
                                anchors.leftMargin: 10
                                anchors.verticalCenter: parent.verticalCenter
                                color: appController.busy
                                    ? window.accent
                                    : (appController.lastOutcome.status === "failed"
                                        ? "#ff6b61" : window.accentSuccess)
                                SequentialAnimation on opacity {
                                    running: appController.busy
                                    loops: Animation.Infinite
                                    onRunningChanged: if (!running) busyDot.opacity = 1.0
                                    NumberAnimation { from: 1.0; to: 0.25; duration: 600 }
                                    NumberAnimation { from: 0.25; to: 1.0; duration: 600 }
                                }
                            }

                            Text {
                                id: runStatusText
                                anchors.left: busyDot.right
                                anchors.leftMargin: 8
                                anchors.right: parent.right
                                anchors.rightMargin: 10
                                anchors.verticalCenter: parent.verticalCenter
                                wrapMode: Text.WordWrap
                                text: appController.busy
                                    ? (appController.progress.requiresUser
                                       ? ("Waiting for user: "
                                          + (appController.progress.waitingReason
                                             || "browser challenge"))
                                       : ((appController.progress.phase || "Starting")
                                       + (appController.progress.total
                                          ? " · " + appController.progress.completed
                                            + "/" + appController.progress.total
                                          : "")))
                                    : ("Last run: "
                                       + (appController.lastOutcome.status || "unknown"))
                                font.family: window.bodyFont
                                font.pixelSize: 11
                                color: appController.lastOutcome.status === "failed"
                                    && !appController.busy
                                    ? "#ff6b61" : "#c7ddec"
                            }
                        }

                        Text {
                            text: "MANUAL STEPS"
                            font.family: window.headingFont
                            font.pixelSize: 10
                            font.letterSpacing: 1.6
                            color: window.textMuted
                            Layout.topMargin: 6
                        }

                        WorkflowStep {
                            stepNumber: "1"
                            text: window.manualVpn ? "VPN ready: collect list" : "VPN List"
                            hint: "Connect your VPN and ensure the app and browser use it. Clicking confirms this network is ready."
                            icon: "\uf0ac"
                            iconFontFamily: iconFont.name
                            accent: window.accent
                            textPrimary: window.textPrimary
                            textMuted: window.textMuted
                            headingFont: window.headingFont
                            bodyFont: window.bodyFont
                            enabled: !appController.busy
                            Layout.fillWidth: true
                            onClicked: appController.startVpnList(
                                window.isStripchat ? false : hideBrowserToggle.checked
                            )
                        }

                        WorkflowStep {
                            stepNumber: "2"
                            text: window.manualVpn ? "Local ready: collect list" : "Local List"
                            hint: "Disconnect or bypass the VPN for the app and browser. Clicking confirms the local network is ready."
                            icon: "\uf015"
                            iconFontFamily: iconFont.name
                            accent: window.accent
                            textPrimary: window.textPrimary
                            textMuted: window.textMuted
                            headingFont: window.headingFont
                            bodyFont: window.bodyFont
                            enabled: !appController.busy
                            Layout.fillWidth: true
                            onClicked: appController.startLocalList(
                                window.isStripchat ? false : hideBrowserToggle.checked
                            )
                        }

                        WorkflowStep {
                            stepNumber: "3"
                            text: "Compare Lists"
                            hint: "Uses saved lists only; no network access."
                            icon: "\uf0ec"
                            iconFontFamily: iconFont.name
                            accent: window.accent
                            textPrimary: window.textPrimary
                            textMuted: window.textMuted
                            headingFont: window.headingFont
                            bodyFont: window.bodyFont
                            enabled: !appController.busy
                            Layout.fillWidth: true
                            onClicked: appController.startCompare()
                        }

                        WorkflowStep {
                            stepNumber: "4"
                            text: window.manualVpn ? "Local ready: verify" : window.step4ButtonText.replace("Run Step 4 - ", "")
                            hint: "Disconnect or bypass the VPN for the app and browser before verifying. Clicking confirms the local network is ready."
                            icon: "\uf058"
                            iconFontFamily: iconFont.name
                            accent: window.accentAlt
                            textPrimary: window.textPrimary
                            textMuted: window.textMuted
                            headingFont: window.headingFont
                            bodyFont: window.bodyFont
                            enabled: !appController.busy
                            showConnector: false
                            Layout.fillWidth: true
                            onClicked: appController.startVerify(minimizeToggle.checked)
                        }

                        AppButton {
                            text: "Stop Current Task"
                            icon: "\uf04d"
                            iconFontFamily: iconFont.name
                            kind: "primary"
                            accent: window.accentDanger
                            fontFamily: window.headingFont
                            enabled: appController.busy
                            Layout.fillWidth: true
                            Layout.topMargin: 4
                            onClicked: appController.stopCurrentTask()
                        }

                        Rectangle {
                            height: 1
                            Layout.fillWidth: true
                            color: window.panelBorder
                            opacity: 0.45
                            Layout.topMargin: 6
                            Layout.bottomMargin: 2
                        }

                        Text {
                            text: "MASTER LIST"
                            font.family: window.headingFont
                            font.pixelSize: 10
                            font.letterSpacing: 1.6
                            color: window.textMuted
                        }

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 10

                            AppButton {
                                text: "View"
                                icon: "\uf022"
                                iconFontFamily: iconFont.name
                                accent: window.accent
                                fontFamily: window.headingFont
                                enabled: !appController.busy
                                Layout.fillWidth: true
                                onClicked: mainStack.currentIndex = 1
                            }

                            AppButton {
                                text: "Compile"
                                icon: "\uf085"
                                iconFontFamily: iconFont.name
                                accent: window.accent
                                fontFamily: window.headingFont
                                enabled: !appController.busy
                                Layout.fillWidth: true
                                onClicked: appController.compileMasterList()
                            }
                        }
                    }
                    }
                }
            }

            SplitView {
                id: rightSplit
                orientation: Qt.Vertical
                SplitView.preferredWidth: window.rightPreferredWidth
                SplitView.minimumWidth: 360
                onWidthChanged: window.markLayoutDirty()
                handle: Item {
                    id: rightHandle
                    implicitWidth: 9
                    implicitHeight: 9
                    Rectangle {
                        anchors.centerIn: parent
                        width: 44
                        height: 2
                        radius: 1
                        color: window.accent
                        opacity: rightHandle.SplitHandle.pressed ? 1.0
                            : (rightHandle.SplitHandle.hovered ? 0.75 : 0.18)
                        Behavior on opacity { NumberAnimation { duration: 120 } }
                    }
                }

                Panel {
                    id: activityPanel
                    SplitView.preferredHeight: window.activityPreferredHeight
                    SplitView.minimumHeight: 220
                    panelColor: window.panelColor
                    borderColor: window.panelBorder
                    accentColor: window.accent
                    title: "Activity Log"
                    titleIcon: "\uf120"
                    titleIconFont: iconFont.name
                    titleFont: window.headingFont
                    titleColor: window.textPrimary
                    onHeightChanged: window.markLayoutDirty()

                    ScrollView {
                        id: logScroll
                        anchors.fill: parent
                        clip: true
                        rightPadding: logVBar.size < 1.0 ? logVBar.width + 4 : 0
                        bottomPadding: logHBar.size < 1.0 ? logHBar.height + 4 : 0
                        background: Rectangle {
                            color: "#020b12"
                            radius: 8
                            border.color: Qt.rgba(1, 1, 1, 0.05)
                            border.width: 1
                        }
                        ScrollBar.vertical: AppScrollBar {
                            id: logVBar
                            accent: window.accent
                            parent: logScroll
                            x: logScroll.width - width
                            height: logScroll.availableHeight
                        }
                        ScrollBar.horizontal: AppScrollBar {
                            id: logHBar
                            accent: window.accent
                            parent: logScroll
                            y: logScroll.height - height
                            width: logScroll.availableWidth
                        }
                        TextArea {
                            id: logArea
                            readOnly: true
                            wrapMode: TextEdit.NoWrap
                            font.family: window.monoFont
                            font.pixelSize: 12
                            font.letterSpacing: 0
                            color: "#a9c4d6"
                            selectByMouse: true
                            selectionColor: Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.35)
                            padding: 12
                            background: null
                        }
                    }
                }

                Panel {
                    id: blockedPanel
                    SplitView.preferredHeight: window.blockedPreferredHeight
                    SplitView.minimumHeight: 160
                    panelColor: window.panelColor
                    borderColor: window.panelBorder
                    accentColor: window.accentAlt
                    title: window.blockedTitle
                    titleIcon: "\uf05e"
                    titleIconFont: iconFont.name
                    titleFont: window.headingFont
                    titleColor: window.textPrimary
                    titleAccent: window.accentAlt
                    onHeightChanged: window.markLayoutDirty()

                    headerExtra: Rectangle {
                        id: blockedCountPill
                        height: 20
                        radius: 10
                        color: window.panelColorDeep
                        border.color: Qt.rgba(1, 1, 1, 0.08)
                        border.width: 1
                        width: blockedCountText.implicitWidth + 18

                        Text {
                            id: blockedCountText
                            anchors.centerIn: parent
                            text: window.modelSearchText.trim().length > 0
                                ? window.filteredModelCount + " / " + appController.blockedModels.length
                                : appController.blockedModels.length + " total"
                            font.family: window.bodyFont
                            font.pixelSize: 10
                            font.letterSpacing: 0.4
                            color: window.textMuted
                        }
                    }

                    ColumnLayout {
                        anchors.fill: parent
                        spacing: 10

                        RowLayout {
                            Layout.fillWidth: true
                            spacing: 8

                            Rectangle {
                                id: modelSearchContainer
                                Layout.fillWidth: true
                                Layout.minimumWidth: 110
                                Layout.preferredHeight: 30
                                radius: 6
                                color: window.panelColorDeep
                                border.color: modelSearchInput.activeFocus
                                    ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.7)
                                    : Qt.rgba(1, 1, 1, 0.08)
                                border.width: 1
                                Behavior on border.color { ColorAnimation { duration: 120 } }

                                Text {
                                    id: modelSearchIcon
                                    anchors.left: parent.left
                                    anchors.leftMargin: 9
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: "\uf002"
                                    font.family: iconFont.name
                                    font.pixelSize: 10
                                    color: window.textMuted
                                }

                                TextInput {
                                    id: modelSearchInput
                                    anchors.fill: parent
                                    anchors.margins: 6
                                    anchors.leftMargin: 26
                                    font.family: window.bodyFont
                                    font.pixelSize: 11
                                    verticalAlignment: Text.AlignVCenter
                                    color: window.textPrimary
                                    selectionColor: window.accent
                                    selectedTextColor: "#03101a"
                                    selectByMouse: true
                                    clip: true
                                    cursorDelegate: Rectangle {
                                        width: 1
                                        color: window.textPrimary
                                    }
                                }

                                Text {
                                    anchors.left: modelSearchInput.left
                                    anchors.verticalCenter: modelSearchInput.verticalCenter
                                    text: "Search models"
                                    font.family: window.bodyFont
                                    font.pixelSize: 11
                                    color: window.textMuted
                                    visible: modelSearchInput.text.length === 0
                                }
                            }

                            AppButton {
                                text: "Load List"
                                icon: "\uf019"
                                iconFontFamily: iconFont.name
                                accent: window.accent
                                fontFamily: window.headingFont
                                compact: true
                                enabled: !appController.busy
                                Layout.fillWidth: true
                                Layout.preferredWidth: 100
                                Layout.minimumWidth: 56
                                Layout.maximumWidth: 100
                                Layout.preferredHeight: 30
                                Layout.alignment: Qt.AlignVCenter
                                onClicked: appController.loadBlockedList()
                            }

                            AppButton {
                                text: "Load Latest"
                                icon: "\uf1da"
                                iconFontFamily: iconFont.name
                                accent: window.accent
                                fontFamily: window.headingFont
                                compact: true
                                enabled: !appController.busy
                                Layout.fillWidth: true
                                Layout.preferredWidth: 108
                                Layout.minimumWidth: 56
                                Layout.maximumWidth: 108
                                Layout.preferredHeight: 30
                                Layout.alignment: Qt.AlignVCenter
                                onClicked: appController.loadLatestBlockedList()
                            }

                            AppButton {
                                text: "Open Next " + window.openBatchSize
                                icon: "\uf35d"
                                iconFontFamily: iconFont.name
                                accent: window.accentAlt
                                fontFamily: window.headingFont
                                compact: true
                                enabled: (window.pinnedVisibleModels.length + window.unpinnedModels.length) > 0
                                Layout.fillWidth: true
                                Layout.preferredWidth: 116
                                Layout.minimumWidth: 56
                                Layout.maximumWidth: 116
                                Layout.preferredHeight: 30
                                Layout.alignment: Qt.AlignVCenter
                                onClicked: window.openNextBatch()
                            }
                        }

                        ColumnLayout {
                            Layout.fillWidth: true
                            Layout.fillHeight: true
                            spacing: 8

                            Rectangle {
                                id: pinnedContainer
                                Layout.fillWidth: true
                                Layout.preferredHeight: window.pinnedVisibleModels.length > 0
                                    ? Math.min(window.pinnedSectionMaxHeight, pinnedList.contentHeight + 52)
                                    : 0
                                Layout.maximumHeight: window.pinnedSectionMaxHeight
                                radius: 6
                                color: window.panelColorDeep
                                border.color: window.panelBorder
                                border.width: 1
                                clip: true
                                visible: window.pinnedVisibleModels.length > 0

                                Column {
                                    anchors.fill: parent
                                    anchors.margins: 8
                                    spacing: 6

                                    Row {
                                        id: pinnedHeader
                                        spacing: 8

                                        Text {
                                            text: "Pinned"
                                            font.family: window.headingFont
                                            font.pixelSize: 12
                                            font.letterSpacing: 0
                                            color: window.textMuted
                                        }

                                        Rectangle {
                                            id: pinnedCountPill
                                            height: 18
                                            radius: 5
                                            color: "#020910"
                                            border.color: window.panelBorder
                                            border.width: 1
                                            width: pinnedCountText.implicitWidth + 12

                                            Text {
                                                id: pinnedCountText
                                                anchors.centerIn: parent
                                                text: window.pinnedVisibleModels.length + " pinned"
                                                font.family: window.bodyFont
                                                font.pixelSize: 9
                                                font.letterSpacing: 0
                                                color: window.textMuted
                                            }
                                        }
                                    }

                                    ListView {
                                        id: pinnedList
                                        width: parent.width
                                        height: Math.min(window.pinnedSectionMaxHeight - 52, contentHeight)
                                        clip: true
                                        model: window.pinnedVisibleModels
                                        spacing: 6
                                        property bool pinnedList: true
                                        property int scrollGutter: pinnedListBar.size < 1.0 ? pinnedListBar.width + 6 : 0
                                        delegate: blockedModelDelegate
                                        ScrollBar.vertical: AppScrollBar { id: pinnedListBar; accent: window.accent }
                                    }
                                }
                            }

                            Rectangle {
                                id: blockedListContainer
                                Layout.fillWidth: true
                                Layout.fillHeight: true
                                radius: 8
                                color: "#020b12"
                                border.color: Qt.rgba(1, 1, 1, 0.05)
                                border.width: 1
                                clip: true

                                ListView {
                                    id: blockedList
                                    anchors.fill: parent
                                    anchors.margins: 8
                                    clip: true
                                    model: window.unpinnedModels
                                    spacing: 6
                                    visible: window.unpinnedModels.length > 0
                                    property bool pinnedList: false
                                    property int scrollGutter: blockedListBar.size < 1.0 ? blockedListBar.width + 6 : 0
                                    delegate: blockedModelDelegate
                                    ScrollBar.vertical: AppScrollBar { id: blockedListBar; accent: window.accent }
                                }

                                Column {
                                    anchors.centerIn: parent
                                    visible: window.pinnedVisibleModels.length === 0 && window.unpinnedModels.length === 0
                                    spacing: 10

                                    Image {
                                        width: Math.min(92, blockedListContainer.height * 0.42)
                                        height: width
                                        anchors.horizontalCenter: parent.horizontalCenter
                                        source: "assets/generated/signal-lab-empty-state.png"
                                        fillMode: Image.PreserveAspectFit
                                        opacity: 0.6
                                        cache: true
                                        asynchronous: true
                                    }

                                    Text {
                                        width: 240
                                        horizontalAlignment: Text.AlignHCenter
                                        text: window.modelSearchText.trim().length > 0
                                            ? "No matching models"
                                            : "No blocked models yet"
                                        font.family: window.headingFont
                                        font.pixelSize: 13
                                        font.letterSpacing: 0.4
                                        color: "#c9dcea"
                                    }

                                    Text {
                                        width: 240
                                        horizontalAlignment: Text.AlignHCenter
                                        text: window.modelSearchText.trim().length > 0
                                            ? "Try a different search term."
                                            : "Run the workflow or load a saved list to get started."
                                        font.family: window.bodyFont
                                        font.pixelSize: 11
                                        color: window.textMuted
                                        wrapMode: Text.WordWrap
                                    }
                                }
                            }
                        }
                    }
                }
            }
        }
    } // End scraperView

            // --- INDEX 1: MASTER LIST ---
            Item {
                id: masterListViewRoot
                Layout.fillWidth: true
                Layout.fillHeight: true

                Image {
                    anchors.fill: parent
                    source: "assets/generated/signal-lab-background.png"
                    fillMode: Image.PreserveAspectCrop
                    opacity: 0.64
                    cache: true
                    asynchronous: true
                }

                Rectangle {
                    anchors.fill: parent
                    color: "#aa02070d"
                }

                ColumnLayout {
                    anchors.fill: parent
                    anchors.margins: 40
                    spacing: 24

                    // Header Area
                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 16

                        ColumnLayout {
                            Layout.fillWidth: true
                            spacing: 4
                            Text {
                                text: "Master Signal Index"
                                font.family: window.headingFont
                                font.pixelSize: 28
                                font.weight: Font.DemiBold
                                color: window.textPrimary
                            }
                            Text {
                                text: "Managing blocked models for " + window.platformTitle
                                font.family: window.bodyFont
                                font.pixelSize: 14
                                color: window.textMuted
                            }
                        }

                        AppButton {
                            text: "Back to Dashboard"
                            icon: "\uf060"
                            iconFontFamily: iconFont.name
                            kind: "ghost"
                            accent: window.accent
                            fontFamily: window.headingFont
                            Layout.preferredHeight: 36
                            Layout.preferredWidth: 168
                            onClicked: mainStack.currentIndex = 0
                        }
                    }

                    // Main Content Panel
                    Panel {
                        Layout.fillWidth: true
                        Layout.fillHeight: true
                        panelColor: window.panelColor
                        borderColor: window.panelBorder
                        accentColor: window.accent

                        ColumnLayout {
                            anchors.fill: parent
                            anchors.margins: 20
                            spacing: 16

                            // Manual Add Area
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 12

                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 40
                                    radius: 8
                                    color: "#03101a"
                                    border.color: manualAddInput.activeFocus
                                        ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.7)
                                        : Qt.rgba(1, 1, 1, 0.08)
                                    border.width: 1
                                    Behavior on border.color { ColorAnimation { duration: 120 } }

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.margins: 10
                                        spacing: 8

                                        Text {
                                            text: "\uf007"
                                            font.family: iconFont.name
                                            font.pixelSize: 13
                                            color: window.textMuted
                                        }

                                        TextField {
                                            id: manualAddInput
                                            Layout.fillWidth: true
                                            placeholderText: "Enter username to manually add..."
                                            placeholderTextColor: window.textMuted
                                            font.family: window.bodyFont
                                            font.pixelSize: 14
                                            color: window.textPrimary
                                            background: null
                                            selectByMouse: true
                                            onAccepted: {
                                                appController.addManualToMaster(manualAddInput.text)
                                                manualAddInput.text = ""
                                            }
                                        }
                                    }
                                }

                                AppButton {
                                    text: "Add Model"
                                    icon: "\uf067"
                                    iconFontFamily: iconFont.name
                                    kind: "primary"
                                    accent: window.accent
                                    fontFamily: window.headingFont
                                    Layout.preferredHeight: 40
                                    Layout.preferredWidth: 120
                                    enabled: manualAddInput.text.trim().length > 0
                                    onClicked: {
                                        appController.addManualToMaster(manualAddInput.text)
                                        manualAddInput.text = ""
                                    }
                                }
                            }

                            Rectangle {
                                Layout.fillWidth: true
                                height: 1
                                color: window.panelBorder
                                opacity: 0.3
                            }

                            // Toolbar
                            RowLayout {
                                Layout.fillWidth: true
                                spacing: 12

                                Rectangle {
                                    Layout.fillWidth: true
                                    Layout.preferredHeight: 40
                                    radius: 8
                                    color: "#03101a"
                                    border.color: masterSearchInput.activeFocus
                                        ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.7)
                                        : Qt.rgba(1, 1, 1, 0.08)
                                    border.width: 1
                                    Behavior on border.color { ColorAnimation { duration: 120 } }

                                    RowLayout {
                                        anchors.fill: parent
                                        anchors.margins: 10
                                        spacing: 8

                                        Text {
                                            text: "\uf002"
                                            font.family: iconFont.name
                                            font.pixelSize: 12
                                            color: window.textMuted
                                        }

                                        TextField {
                                            id: masterSearchInput
                                            Layout.fillWidth: true
                                            placeholderText: "Search models..."
                                            placeholderTextColor: window.textMuted
                                            font.family: window.bodyFont
                                            font.pixelSize: 14
                                            color: window.textPrimary
                                            background: null
                                            selectByMouse: true
                                        }
                                    }
                                }

                                Rectangle {
                                    height: 24
                                    radius: 12
                                    color: window.panelColorDeep
                                    border.color: Qt.rgba(1, 1, 1, 0.08)
                                    border.width: 1
                                    Layout.alignment: Qt.AlignVCenter
                                    Layout.preferredWidth: masterCountText.implicitWidth + 20

                                    Text {
                                        id: masterCountText
                                        anchors.centerIn: parent
                                        text: masterListView.count + " models"
                                        font.family: window.monoFont
                                        font.pixelSize: 11
                                        color: window.textMuted
                                    }
                                }

                                AppButton {
                                    text: "Refresh"
                                    icon: "\uf021"
                                    iconFontFamily: iconFont.name
                                    accent: window.accent
                                    fontFamily: window.headingFont
                                    Layout.preferredHeight: 40
                                    Layout.preferredWidth: 104
                                    onClicked: appController.compileMasterList()
                                }

                                AppButton {
                                    text: window.manualVpn ? "Local ready: verify" : "Verify"
                                    icon: "\uf058"
                                    iconFontFamily: iconFont.name
                                    accent: window.accentAlt
                                    fontFamily: window.headingFont
                                    Layout.preferredHeight: 40
                                    Layout.preferredWidth: window.manualVpn ? 164 : 96
                                    visible: !window.isMfc
                                    enabled: !appController.busy
                                    ToolTip.visible: hovered
                                    ToolTip.text: "Disconnect or bypass VPN for the app and browser. Clicking confirms the local network is ready."
                                    onClicked: appController.verifyMasterList(true)
                                }

                                ComboBox {
                                    id: sortByCombo
                                    Layout.preferredWidth: 168
                                    Layout.preferredHeight: 40
                                    model: ["Name (A-Z)", "Date (Newest)", "Date (Oldest)", "Country"]
                                    currentIndex: 1

                                    readonly property var sortModes: [
                                        "name", "date_newest", "date_oldest", "country"
                                    ]

                                    function syncFromSettings() {
                                        var index = sortModes.indexOf(
                                            appController.settings.masterListSortMode
                                        )
                                        currentIndex = index === -1 ? 1 : index
                                    }

                                    Component.onCompleted: syncFromSettings()

                                    Connections {
                                        target: appController
                                        function onSettingsChanged() {
                                            sortByCombo.syncFromSettings()
                                        }
                                    }

                                    onActivated: function(index) {
                                        appController.sortMasterList(
                                            sortByCombo.sortModes[index]
                                        )
                                    }

                                    indicator: Text {
                                        x: sortByCombo.width - width - 12
                                        y: (sortByCombo.height - height) / 2
                                        text: "\uf078"
                                        font.family: iconFont.name
                                        font.pixelSize: 9
                                        color: sortByCombo.pressed || sortByCombo.hovered
                                            ? window.accent : window.textMuted
                                    }

                                    delegate: ItemDelegate {
                                        width: sortByCombo.width - 8
                                        height: 34
                                        contentItem: Text {
                                            text: modelData
                                            color: highlighted ? window.textPrimary : window.textMuted
                                            font.family: window.bodyFont
                                            font.pixelSize: 13
                                            verticalAlignment: Text.AlignVCenter
                                            elide: Text.ElideRight
                                        }
                                        background: Rectangle {
                                            radius: 6
                                            color: highlighted
                                                ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.12)
                                                : "transparent"
                                        }
                                    }

                                    popup: Popup {
                                        y: sortByCombo.height + 4
                                        width: sortByCombo.width
                                        implicitHeight: contentItem.implicitHeight + 8
                                        padding: 4

                                        contentItem: ListView {
                                            clip: true
                                            implicitHeight: contentHeight
                                            model: sortByCombo.popup.visible ? sortByCombo.delegateModel : null
                                            currentIndex: sortByCombo.highlightedIndex
                                            ScrollBar.vertical: AppScrollBar { accent: window.accent }
                                        }

                                        background: Rectangle {
                                            color: "#050f18"
                                            border.color: window.panelBorderStrong
                                            border.width: 1
                                            radius: 10
                                        }
                                    }

                                    contentItem: Text {
                                        leftPadding: 12
                                        rightPadding: 28
                                        text: "Sort: " + sortByCombo.displayText
                                        font.family: window.bodyFont
                                        font.pixelSize: 13
                                        color: window.textPrimary
                                        verticalAlignment: Text.AlignVCenter
                                        elide: Text.ElideRight
                                    }

                                    background: Rectangle {
                                        color: sortByCombo.hovered ? "#071c2b" : "#03101a"
                                        border.color: sortByCombo.popup.visible
                                            ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.7)
                                            : Qt.rgba(1, 1, 1, 0.08)
                                        border.width: 1
                                        radius: 8
                                        Behavior on color { ColorAnimation { duration: 120 } }
                                    }
                                }

                                ComboBox {
                                    id: countryFilterCombo
                                    Layout.preferredWidth: 158
                                    Layout.preferredHeight: 40
                                    model: {
                                        var m = [{ name: "All Countries", code: "", flag: "\uf024" }]
                                        for(var i=1; i<window.countries.length; i++) {
                                            m.push(window.countries[i])
                                        }
                                        return m
                                    }

                                    function syncFromSettings() {
                                        var saved = String(
                                            appController.settings.masterListCountryFilter || "All"
                                        ).toUpperCase()
                                        if (saved === "ALL") {
                                            currentIndex = 0
                                            return
                                        }
                                        for (var i = 1; i < model.length; i++) {
                                            if (model[i].code.toUpperCase() === saved) {
                                                currentIndex = i
                                                return
                                            }
                                        }
                                        currentIndex = 0
                                    }

                                    Component.onCompleted: syncFromSettings()

                                    Connections {
                                        target: appController
                                        function onSettingsChanged() {
                                            countryFilterCombo.syncFromSettings()
                                        }
                                    }

                                    onActivated: function(index) {
                                        if (index === 0) appController.setMasterListCountryFilter("All")
                                        else appController.setMasterListCountryFilter(model[index].code.toUpperCase())
                                    }

                                    indicator: Text {
                                        x: countryFilterCombo.width - width - 12
                                        y: (countryFilterCombo.height - height) / 2
                                        text: "\uf078"
                                        font.family: iconFont.name
                                        font.pixelSize: 9
                                        color: countryFilterCombo.pressed || countryFilterCombo.hovered
                                            ? window.accent : window.textMuted
                                    }

                                    delegate: ItemDelegate {
                                        width: countryFilterCombo.width - 8
                                        height: 34
                                        padding: 8
                                        contentItem: RowLayout {
                                            spacing: 10
                                            Image {
                                                id: filterFlagImg
                                                source: (modelData && modelData.flagImage) ? modelData.flagImage : ""
                                                Layout.preferredWidth: 20
                                                Layout.preferredHeight: 14
                                                visible: status === Image.Ready
                                                fillMode: Image.PreserveAspectFit
                                            }
                                            Text {
                                                text: modelData ? modelData.flag : ""
                                                font.family: window.bodyFont
                                                font.pixelSize: 13
                                                color: window.textMuted
                                                visible: filterFlagImg.status !== Image.Ready
                                            }
                                            Text {
                                                text: modelData ? modelData.name : ""
                                                color: highlighted ? window.textPrimary : window.textMuted
                                                font.family: window.bodyFont
                                                font.pixelSize: 13
                                                Layout.fillWidth: true
                                                elide: Text.ElideRight
                                            }
                                        }
                                        background: Rectangle {
                                            radius: 6
                                            color: highlighted
                                                ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.12)
                                                : "transparent"
                                        }
                                    }

                                    popup: Popup {
                                        y: countryFilterCombo.height + 4
                                        width: countryFilterCombo.width
                                        implicitHeight: Math.min(contentItem.implicitHeight + 8, 380)
                                        padding: 4

                                        contentItem: ListView {
                                            clip: true
                                            implicitHeight: contentHeight
                                            model: countryFilterCombo.popup.visible ? countryFilterCombo.delegateModel : null
                                            currentIndex: countryFilterCombo.highlightedIndex
                                            ScrollBar.vertical: AppScrollBar { accent: window.accent }
                                        }

                                        background: Rectangle {
                                            color: "#050f18"
                                            border.color: window.panelBorderStrong
                                            border.width: 1
                                            radius: 10
                                        }
                                    }

                                    displayText: currentIndex === 0 ? "All Countries" : model[currentIndex].name

                                    contentItem: Text {
                                        leftPadding: 12
                                        rightPadding: 28
                                        text: "Filter: " + countryFilterCombo.displayText
                                        font.family: window.bodyFont
                                        font.pixelSize: 13
                                        color: window.textPrimary
                                        verticalAlignment: Text.AlignVCenter
                                        elide: Text.ElideRight
                                    }

                                    background: Rectangle {
                                        color: countryFilterCombo.hovered ? "#071c2b" : "#03101a"
                                        border.color: countryFilterCombo.popup.visible
                                            ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.7)
                                            : Qt.rgba(1, 1, 1, 0.08)
                                        border.width: 1
                                        radius: 8
                                        Behavior on color { ColorAnimation { duration: 120 } }
                                    }
                                }
                            }

                            // List
                            Rectangle {
                                Layout.fillWidth: true
                                Layout.fillHeight: true /*id:1193*/
                                color: "#020b12"
                                radius: 8
                                border.color: Qt.rgba(1, 1, 1, 0.05)
                                border.width: 1
                                clip: true

                                ListView {
                                    id: masterListView
                                    anchors.fill: parent
                                    anchors.margins: 8
                                    clip: true
                                    property int scrollGutter: masterListBar.size < 1.0 ? masterListBar.width + 6 : 0
                                    model: {
                                        var all = appController.masterListModels
                                        var query = window.normalizeSearchText(masterSearchInput.text)
                                        if (!query) return all

                                        var filtered = []
                                        for (var i = 0; i < all.length; i++) {
                                            if (window.modelMatchesSearch(all[i].name, query)) {
                                                filtered.push(all[i])
                                            }
                                        }
                                        return filtered
                                    }
                                    delegate: Item {
                                        id: wrapper
                                        width: ListView.view.width - (ListView.view.scrollGutter || 0)
                                        height: 44
                                        property bool hovered: rowMouseArea.containsMouse || masterExternalSearchArea.containsMouse || masterCamGirlFinderArea.containsMouse || pinArea.containsMouse || deleteArea.containsMouse || profileBadgeArea.containsMouse || blockedProfileBadgeArea.containsMouse || countrySelector.activeFocus
                                        property bool selected: window.selectedMasterModel === modelData.name
                                        property bool profileMatched: modelData.location_profile_match === true
                                        property bool blockedProfileMatched: modelData.vpn_location_profile_match === true
                                        property string profileDetails: {
                                            var evidence = modelData.profile_match || {}
                                            var details = ["Profile location matches your preferences"]
                                            if (evidence.location) details.push("Location: " + evidence.location)
                                            if (evidence.country) details.push("Country: " + evidence.country)
                                            if (evidence.match_reasons) details.push("Matched by: " + evidence.match_reasons.join(", "))
                                            if (modelData.verification_reason) details.push("Observed reason: " + modelData.verification_reason)
                                            return details.join("\n")
                                        }
                                        property string blockedProfileDetails: {
                                            var evidence = modelData.vpn_profile_match || {}
                                            var details = ["Matching profile with an observed access restriction"]
                                            if (evidence.location) details.push("Location: " + evidence.location)
                                            if (evidence.country) details.push("Country: " + evidence.country)
                                            if (evidence.match_reasons) details.push("Matched by: " + evidence.match_reasons.join(", "))
                                            if (modelData.verification_reason) details.push("Observed reason: " + modelData.verification_reason)
                                            return details.join("\n")
                                        }
                                        property string username: {
                                            var t = String(modelData.name || "").trim()
                                            if (t.endsWith("/")) t = t.slice(0, -1)
                                            var parts = t.split("/")
                                            var last = parts[parts.length - 1]
                                            if (last.indexOf("#") === 0) last = last.slice(1)
                                            return last
                                        }

                                        // 1. Background layer
                                        Rectangle {
                                            anchors.fill: parent
                                            anchors.margins: 2
                                            color: wrapper.selected
                                                ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.13)
                                                : (wrapper.blockedProfileMatched ? "#190e14" : (wrapper.profileMatched ? "#17140d" : (index % 2 === 0 ? "transparent" : Qt.rgba(1, 1, 1, 0.02))))
                                            radius: 6
                                            border.color: wrapper.selected
                                                ? Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.65)
                                                : (wrapper.blockedProfileMatched ? "#4d843445" : (wrapper.profileMatched ? "#4d7a5720" : "transparent"))
                                            border.width: 1
                                        }

                                        // 2. Hover Highlight layer
                                        Rectangle {
                                            anchors.fill: parent
                                            anchors.margins: 2
                                            color: window.accent
                                            opacity: (wrapper.hovered && !wrapper.selected) ? 0.08 : 0
                                            radius: 6
                                            Behavior on opacity { NumberAnimation { duration: 150 } }
                                        }

                                        // New Model Highlight
                                        Rectangle {
                                            anchors.fill: parent
                                            anchors.margins: 2
                                            color: window.accent
                                            opacity: (modelData.is_new === true) ? 0.05 : 0
                                            radius: 6
                                            visible: !!modelData.is_new
                                        }

                                        // 3. Row Layout
                                        RowLayout {
                                            anchors.fill: parent
                                            anchors.leftMargin: 12
                                            anchors.rightMargin: 12
                                            spacing: 12

                                            // Country Selector/Flag
                                            ComboBox {
                                                id: countrySelector
                                                Layout.preferredWidth: 44
                                                Layout.preferredHeight: 32
                                                model: window.countries
                                                textRole: "flag"

                                                currentIndex: {
                                                    var code = (modelData.country || "").toLowerCase()
                                                    for (var i = 0; i < window.countries.length; i++) {
                                                        if (window.countries[i].code === code) return i
                                                    }
                                                    return 0
                                                }

                                                onActivated: function(index) {
                                                    appController.updateMasterModelCountry(modelData.name, window.countries[index].code.toUpperCase())
                                                }

                                                background: Rectangle {
                                                    color: wrapper.hovered ? "#09283b" : "transparent"
                                                    border.color: countrySelector.activeFocus || countrySelector.hovered ? window.accent : "transparent"
                                                    border.width: 1
                                                    radius: 6
                                                    opacity: 1
                                                }

                                                indicator: null

                                                contentItem: Item {
                                                    anchors.fill: parent
                                                    Image {
                                                        id: flagImg
                                                        anchors.centerIn: parent
                                                        source: countrySelector.selectedItem ? countrySelector.selectedItem.flagImage || "" : ""
                                                        width: 24
                                                        height: 16
                                                        visible: status === Image.Ready
                                                        fillMode: Image.PreserveAspectFit
                                                    }
                                                    Text {
                                                        anchors.centerIn: parent
                                                        text: countrySelector.selectedItem ? countrySelector.selectedItem.flag : "\uf024"
                                                        font.family: window.bodyFont
                                                        font.pixelSize: 13
                                                        color: window.textPrimary
                                                        visible: flagImg.status !== Image.Ready
                                                    }
                                                }

                                                // Custom property to get current data object
                                                property var selectedItem: model[currentIndex]

                                                popup: Popup {
                                                    y: countrySelector.height + 4
                                                    width: 180
                                                    implicitHeight: Math.min(contentItem.implicitHeight, 400)
                                                    padding: 4

                                                    contentItem: ListView {
                                                        clip: true
                                                        implicitHeight: contentHeight
                                                        model: countrySelector.delegateModel
                                                        currentIndex: countrySelector.highlightedIndex
                                                        ScrollBar.vertical: AppScrollBar {
                                                            accent: window.accent
                                                            policy: ScrollBar.AsNeeded
                                                        }
                                                    }

                                                    background: Rectangle {
                                                        color: "#050f18"
                                                        border.color: window.panelBorderStrong
                                                        border.width: 1
                                                        radius: 10
                                                    }
                                                }

                                                delegate: ItemDelegate {
                                                    width: 172
                                                    height: 40
                                                    padding: 8

                                                    contentItem: RowLayout {
                                                        spacing: 12
                                                        Image {
                                                            id: delegateFlagImg
                                                            source: modelData.flagImage || ""
                                                            width: 24
                                                            height: 16
                                                            visible: status === Image.Ready
                                                            fillMode: Image.PreserveAspectFit
                                                            Layout.alignment: Qt.AlignVCenter
                                                        }
                                                        Text {
                                                            text: modelData.flag
                                                            font.family: window.bodyFont
                                                            font.pixelSize: 13
                                                            color: window.textMuted
                                                            visible: delegateFlagImg.status !== Image.Ready
                                                            Layout.alignment: Qt.AlignVCenter
                                                        }
                                                        Text {
                                                            text: modelData.name
                                                            color: highlighted ? window.accent : window.textPrimary
                                                            font.family: window.bodyFont
                                                            font.pixelSize: 14
                                                            Layout.fillWidth: true
                                                            verticalAlignment: Text.AlignVCenter
                                                        }
                                                        Text {
                                                            text: "\uf00c"
                                                            font.family: iconFont.name
                                                            font.pixelSize: 12
                                                            visible: countrySelector.currentIndex === index
                                                            color: window.accent
                                                        }
                                                    }

                                                    background: Rectangle {
                                                        color: highlighted ? "#06364f" : "transparent"
                                                        radius: 6
                                                    }
                                                }
                                            }

                                            // Username (Clickable)
                                            Text {
                                                Layout.fillWidth: true
                                                text: wrapper.username
                                                font.family: window.headingFont
                                                font.pixelSize: 14
                                                font.letterSpacing: 0
                                                color: window.textPrimary
                                                elide: Text.ElideRight

                                                MouseArea {
                                                    anchors.fill: parent
                                                    cursorShape: Qt.PointingHandCursor
                                                    onClicked: {
                                                        window.selectedMasterModel = modelData.name
                                                        var url = window.modelUrlFromText(modelData.name)
                                                        if (url) {
                                                            appController.copyModelReference(
                                                                wrapper.username,
                                                                url
                                                            )
                                                            appController.openModelUrl(url)
                                                        }
                                                    }
                                                }
                                            }

                                            // Date and New Badge
                                            Row {
                                                spacing: 8
                                                Layout.alignment: Qt.AlignVCenter

                                                Text {
                                                    text: {
                                                        var d = (modelData.date === "Unknown") ? "" : (modelData.date || "")
                                                        var timestamp = String(modelData.timestamp || "")
                                                        if (timestamp.length >= 16) {
                                                            d = timestamp.slice(0, 10) + " " + timestamp.slice(11, 16)
                                                        }
                                                        if (d && modelData.manual) return "Manual " + d
                                                        return d
                                                    }
                                                    font.family: window.monoFont
                                                    font.pixelSize: 11
                                                    color: window.textMuted
                                                    visible: modelData.date !== "Unknown"
                                                    anchors.verticalCenter: parent.verticalCenter
                                                }

                                                Rectangle {
                                                    visible: wrapper.blockedProfileMatched
                                                    height: 18
                                                    width: 124
                                                    radius: 4
                                                    color: "#2bff6b7a"
                                                    border.color: "#80ff6b7a"
                                                    border.width: 1
                                                    anchors.verticalCenter: parent.verticalCenter

                                                    Text {
                                                        anchors.centerIn: parent
                                                        text: "RESTRICTED PROFILE"
                                                        font.family: window.headingFont
                                                        font.pixelSize: 9
                                                        font.weight: Font.Bold
                                                        color: "#ff8995"
                                                    }

                                                    ToolTip.visible: blockedProfileBadgeArea.containsMouse
                                                    ToolTip.text: wrapper.blockedProfileDetails
                                                    ToolTip.delay: 300

                                                    MouseArea {
                                                        id: blockedProfileBadgeArea
                                                        anchors.fill: parent
                                                        hoverEnabled: true
                                                    }
                                                }

                                                Rectangle {
                                                    visible: wrapper.profileMatched && !wrapper.blockedProfileMatched
                                                    height: 18
                                                    width: 82
                                                    radius: 4
                                                    color: "#2bffb547"
                                                    border.color: "#80ffb547"
                                                    border.width: 1
                                                    anchors.verticalCenter: parent.verticalCenter

                                                    Text {
                                                        anchors.centerIn: parent
                                                        text: "PROFILE"
                                                        font.family: window.headingFont
                                                        font.pixelSize: 9
                                                        font.weight: Font.Bold
                                                        color: "#ffcb70"
                                                    }

                                                    ToolTip.visible: profileBadgeArea.containsMouse
                                                    ToolTip.text: wrapper.profileDetails
                                                    ToolTip.delay: 300

                                                    MouseArea {
                                                        id: profileBadgeArea
                                                        anchors.fill: parent
                                                        hoverEnabled: true
                                                    }
                                                }

                                                Rectangle {
                                                    visible: !!modelData.is_new
                                                    height: 18
                                                    width: 38
                                                    radius: 4
                                                    color: window.accent
                                                    opacity: 0.2
                                                    anchors.verticalCenter: parent.verticalCenter
                                                    Text {
                                                        anchors.centerIn: parent
                                                        text: "NEW"
                                                        font.family: window.headingFont
                                                        font.pixelSize: 9
                                                        font.weight: Font.Bold
                                                        color: window.accent
                                                    }
                                                }
                                            }

                                            // Action Buttons
                                            Row {
                                                spacing: 6
                                                Layout.alignment: Qt.AlignVCenter

                                                property bool isPinned: window.pinnedModels.indexOf(modelData.name) !== -1

                                                // External Search Button
                                                Rectangle {
                                                    width: 28
                                                    height: 28
                                                    radius: 14
                                                    color: masterExternalSearchArea.containsMouse ? "#0b3445" : "#03101a"
                                                    border.color: masterExternalSearchArea.containsMouse ? window.accent : window.panelBorder
                                                    border.width: 1
                                                    opacity: wrapper.hovered ? 1 : 0
                                                    Behavior on opacity { NumberAnimation { duration: 150 } }

                                                    Text {
                                                        anchors.centerIn: parent
                                                        text: "\uf002"
                                                        font.family: iconFont.name
                                                        font.pixelSize: 12
                                                        color: masterExternalSearchArea.containsMouse ? window.accent : window.textMuted
                                                    }

                                                    ToolTip.visible: masterExternalSearchArea.containsMouse
                                                    ToolTip.text: "Choose where to search " + wrapper.username
                                                    ToolTip.delay: 350

                                                    MouseArea {
                                                        id: masterExternalSearchArea
                                                        anchors.fill: parent
                                                        hoverEnabled: true
                                                        cursorShape: Qt.PointingHandCursor
                                                        onClicked: {
                                                            window.selectedMasterModel = modelData.name
                                                            searchProviderMenu.openFor(
                                                                parent,
                                                                wrapper.username
                                                            )
                                                        }
                                                    }
                                                }

                                                // CamGirlFinder Search Button
                                                Rectangle {
                                                    width: 38
                                                    height: 28
                                                    radius: 14
                                                    color: masterCamGirlFinderArea.containsMouse ? "#421b2a" : "#03101a"
                                                    border.color: masterCamGirlFinderArea.containsMouse ? "#f05b78" : window.panelBorder
                                                    border.width: 1
                                                    opacity: wrapper.hovered ? 1 : 0
                                                    Behavior on opacity { NumberAnimation { duration: 150 } }

                                                    Text {
                                                        anchors.centerIn: parent
                                                        text: "CGF"
                                                        font.family: window.headingFont
                                                        font.pixelSize: 9
                                                        font.bold: true
                                                        color: masterCamGirlFinderArea.containsMouse ? "#ff8399" : window.textMuted
                                                    }

                                                    ToolTip.visible: masterCamGirlFinderArea.containsMouse
                                                    ToolTip.text: "Search " + wrapper.username + " on CamGirlFinder"
                                                    ToolTip.delay: 350

                                                    MouseArea {
                                                        id: masterCamGirlFinderArea
                                                        anchors.fill: parent
                                                        hoverEnabled: true
                                                        cursorShape: Qt.PointingHandCursor
                                                        onClicked: {
                                                            window.selectedMasterModel = modelData.name
                                                            appController.openExternalSearch("camgirlfinder", wrapper.username)
                                                        }
                                                    }
                                                }

                                                // Pin Button
                                                Rectangle {
                                                    width: 28
                                                    height: 28
                                                    radius: 14
                                                    color: parent.isPinned ? window.accent : "#03101a"
                                                    border.color: parent.isPinned ? window.accent : window.panelBorder
                                                    border.width: 1
                                                    opacity: parent.isPinned || wrapper.hovered ? 1 : 0
                                                    Behavior on opacity { NumberAnimation { duration: 150 } }

                                                    Text {
                                                        anchors.centerIn: parent
                                                        text: "\uf08d" // pin
                                                        font.family: iconFont.name
                                                        font.pixelSize: 13
                                                        color: parent.isPinned ? "#03101a" : window.textMuted
                                                    }

                                                    MouseArea {
                                                        id: pinArea
                                                        anchors.fill: parent
                                                        cursorShape: Qt.PointingHandCursor
                                                        onClicked: window.togglePinnedModel(modelData.name)
                                                    }
                                                }

                                                // Delete Button
                                                Rectangle {
                                                    width: 28
                                                    height: 28
                                                    radius: 14
                                                    color: "#03101a"
                                                    border.color: deleteArea.containsMouse ? "#ff4d3d" : window.panelBorder
                                                    border.width: 1
                                                    opacity: wrapper.hovered ? 1 : 0
                                                    Behavior on opacity { NumberAnimation { duration: 150 } }

                                                    Text {
                                                        anchors.centerIn: parent
                                                        text: "\uf1f8" // trash-can
                                                        font.family: iconFont.name
                                                        font.pixelSize: 13
                                                        color: deleteArea.containsMouse ? "#ff4d3d" : "#7f98aa"
                                                    }

                                                    MouseArea {
                                                        id: deleteArea
                                                        anchors.fill: parent
                                                        hoverEnabled: true
                                                        cursorShape: Qt.PointingHandCursor
                                                        onClicked: {
                                                            if (appController.settings.confirmDelete) {
                                                                deleteConfirmPopup.targetModel = modelData.name
                                                                deleteConfirmPopup.open()
                                                            } else {
                                                                appController.deleteFromMasterList(
                                                                    modelData.name,
                                                                    appController.settings.blockOnDelete
                                                                )
                                                            }
                                                        }
                                                    }
                                                }
                                            }
                                        }

                                        // Overlay MouseArea for row selection (without blocking buttons)
                                        MouseArea {
                                            id: rowMouseArea
                                            anchors.fill: parent
                                            hoverEnabled: true
                                            onClicked: window.selectedMasterModel = modelData.name
                                            z: -1 // Behind text and buttons
                                        }
                                    }

                                    ScrollBar.vertical: AppScrollBar {
                                        id: masterListBar
                                        accent: window.accent
                                        policy: ScrollBar.AlwaysOn
                                        active: true
                                    }
                                }
                            }
                        }
                    }
                }
            } // End masterListViewRoot

            // --- INDEX 2: SETTINGS ---
            SettingsView {
                id: settingsView
                objectName: "settingsView"
                Layout.fillWidth: true
                Layout.fillHeight: true
                accent: window.accent
                accentAlt: window.accentAlt
                accentSuccess: window.accentSuccess
                accentWarn: window.accentWarn
                accentDanger: window.accentDanger
                panelColor: window.panelColor
                panelColorDeep: window.panelColorDeep
                panelBorder: window.panelBorder
                panelBorderStrong: window.panelBorderStrong
                textPrimary: window.textPrimary
                textMuted: window.textMuted
                headingFont: window.headingFont
                bodyFont: window.bodyFont
                monoFont: window.monoFont
                iconFont: window.iconFontFamily
                countries: window.countries
                pinnedCount: window.pinnedModels.length
                onBackRequested: mainStack.currentIndex = 0
                onResetLayoutRequested: window.resetLayout()
                onClearPinnedRequested: window.clearPinnedModels()
            }

        } // End StackLayout
    } // End mainLayout

    Popup {
        id: deleteConfirmPopup
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        width: 380
        height: deleteLayout.implicitHeight + 40
        x: Math.round((window.width - width) / 2)
        y: Math.round((window.height - height) / 2)

        property string targetModel: ""

        onOpened: blockCheckbox.checked = appController.settings.blockOnDelete

        Overlay.modal: Rectangle {
            color: "#cc02070d"
        }

        enter: Transition {
            ParallelAnimation {
                NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: 160; easing.type: Easing.OutCubic }
                NumberAnimation { property: "scale"; from: 0.96; to: 1.0; duration: 180; easing.type: Easing.OutCubic }
            }
        }
        exit: Transition {
            NumberAnimation { property: "opacity"; from: 1.0; to: 0.0; duration: 100 }
        }

        background: Rectangle {
            color: "#050f18"
            border.color: Qt.rgba(window.accentDanger.r, window.accentDanger.g, window.accentDanger.b, 0.45)
            border.width: 1
            radius: 12
        }

        ColumnLayout {
            id: deleteLayout
            anchors.fill: parent
            anchors.margins: 20
            spacing: 14

            RowLayout {
                spacing: 10

                Rectangle {
                    Layout.preferredWidth: 34
                    Layout.preferredHeight: 34
                    radius: 8
                    color: Qt.rgba(window.accentDanger.r, window.accentDanger.g, window.accentDanger.b, 0.12)
                    border.color: Qt.rgba(window.accentDanger.r, window.accentDanger.g, window.accentDanger.b, 0.35)
                    border.width: 1

                    Text {
                        anchors.centerIn: parent
                        text: "\uf071"
                        font.family: iconFont.name
                        font.pixelSize: 13
                        color: window.accentDanger
                    }
                }

                Text {
                    text: "Delete Model?"
                    font.family: window.headingFont
                    font.pixelSize: 18
                    font.weight: Font.DemiBold
                    color: window.textPrimary
                }
            }

            Text {
                text: "Are you sure you want to remove <b>" + deleteConfirmPopup.targetModel + "</b> from the master list?"
                font.family: window.bodyFont
                font.pixelSize: 13
                color: window.textMuted
                wrapMode: Text.Wrap
                Layout.fillWidth: true
                textFormat: Text.RichText
            }

            AppToggle {
                id: blockCheckbox
                text: "Block permanently (blacklist)"
                accent: window.accentDanger
                fontFamily: window.bodyFont
                checked: false
                Layout.fillWidth: true
            }

            RowLayout {
                Layout.fillWidth: true
                Layout.topMargin: 4
                spacing: 12

                AppButton {
                    text: "Cancel"
                    kind: "ghost"
                    accent: window.accent
                    fontFamily: window.headingFont
                    Layout.fillWidth: true
                    Layout.preferredHeight: 36
                    onClicked: deleteConfirmPopup.close()
                }

                AppButton {
                    text: "Delete"
                    icon: "\uf1f8" // trash-can
                    iconFontFamily: iconFont.name
                    kind: "primary"
                    accent: window.accentDanger
                    fontFamily: window.headingFont
                    Layout.fillWidth: true
                    Layout.preferredHeight: 36
                    onClicked: {
                        appController.deleteFromMasterList(deleteConfirmPopup.targetModel, blockCheckbox.checked)
                        deleteConfirmPopup.close()
                    }
                }
            }
        }
    }

    Connections {
        target: appController
        function onBlockedModelsChanged() {
            window.rebuildModelLists()
        }
        function onLogMessage(message) {
            logArea.append(message)
            if (appController.settings.logAutoScroll) {
                logArea.cursorPosition = logArea.length
            }
        }
    }

    Popup {
        id: sessionSummaryPopup
        modal: true
        focus: true
        closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside
        width: 400
        height: 320
        x: Math.round((window.width - width) / 2)
        y: Math.round((window.height - height) / 2)

        property int newModelsCount: 0
        property int totalModelsCount: 0

        Overlay.modal: Rectangle {
            color: "#cc02070d"
        }

        background: Rectangle {
            color: "#050f18"
            border.color: Qt.rgba(window.accent.r, window.accent.g, window.accent.b, 0.55)
            border.width: 1
            radius: 12

            // Outer glow
            Rectangle {
                anchors.fill: parent
                anchors.margins: -4
                radius: 14
                color: "transparent"
                border.color: window.accent
                border.width: 1
                opacity: 0.18
            }
        }

        ColumnLayout {
            anchors.fill: parent
            anchors.margins: 24
            spacing: 20

            // Header Area
            RowLayout {
                Layout.fillWidth: true
                spacing: 12

                Rectangle {
                    width: 40
                    height: 40
                    radius: 8
                    color: window.accent
                    opacity: 0.1

                    Text {
                        anchors.centerIn: parent
                        text: "\uf080" // chart-bar
                        font.family: iconFont.name
                        font.pixelSize: 20
                        color: window.accent
                    }
                }

                ColumnLayout {
                    spacing: 0
                    Text {
                        text: "SESSION REPORT"
                        font.family: window.headingFont
                        font.pixelSize: 20
                        font.letterSpacing: 1.5
                        color: window.accent
                        font.weight: Font.Bold
                    }
                    Text {
                        text: "SCANNING & COMPILATION COMPLETE"
                        font.family: window.bodyFont
                        font.pixelSize: 10
                        font.letterSpacing: 1
                        color: window.textMuted
                    }
                }
            }

            Rectangle {
                Layout.fillWidth: true
                height: 1
                color: window.panelBorder
                opacity: 0.5
            }

            // Stats Area
            RowLayout {
                Layout.fillWidth: true
                spacing: 24

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 4
                    Text {
                        text: "+" + sessionSummaryPopup.newModelsCount
                        font.family: window.headingFont
                        font.pixelSize: 42
                        color: sessionSummaryPopup.newModelsCount > 0 ? window.accentAlt : window.textMuted
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "NEW TARGETS"
                        font.family: window.bodyFont
                        font.pixelSize: 12
                        font.letterSpacing: 1
                        color: window.textMuted
                        Layout.alignment: Qt.AlignHCenter
                    }
                }

                Rectangle {
                    width: 1
                    Layout.fillHeight: true
                    color: window.panelBorder
                    opacity: 0.5
                }

                ColumnLayout {
                    Layout.fillWidth: true
                    spacing: 4
                    Text {
                        text: sessionSummaryPopup.totalModelsCount
                        font.family: window.headingFont
                        font.pixelSize: 42
                        color: window.textPrimary
                        Layout.alignment: Qt.AlignHCenter
                    }
                    Text {
                        text: "TOTAL TRACKED"
                        font.family: window.bodyFont
                        font.pixelSize: 12
                        font.letterSpacing: 1
                        color: window.textMuted
                        Layout.alignment: Qt.AlignHCenter
                    }
                }
            }

            Item { Layout.fillHeight: true }

            AppButton {
                text: "ACKNOWLEDGE"
                icon: "\uf00c" // check
                iconFontFamily: iconFont.name
                accent: window.accent
                fontFamily: window.headingFont
                Layout.fillWidth: true
                Layout.preferredHeight: 44
                onClicked: sessionSummaryPopup.close()
            }
        }

        // Appear animation
        enter: Transition {
            NumberAnimation { property: "opacity"; from: 0.0; to: 1.0; duration: 300; easing.type: Easing.OutQuad }
            NumberAnimation { property: "scale"; from: 0.9; to: 1.0; duration: 300; easing.type: Easing.OutBack }
        }
    }
}
