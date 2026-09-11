import QtQuick 2.15
import QtQuick.Controls 2.15
import QtQuick.Layouts 1.15
import "components"

Item {
    id: root

    property color accent: "#12d6ff"
    property color accentAlt: "#f97316"
    property color accentSuccess: "#39e75f"
    property color accentWarn: "#f59e0b"
    property color accentDanger: "#ff4d3d"
    property color panelColor: "#061521"
    property color panelColorDeep: "#03101a"
    property color panelBorder: "#12394a"
    property color panelBorderStrong: "#1f6b88"
    property color textPrimary: "#eef8ff"
    property color textMuted: "#7f98aa"
    property string headingFont: "Bahnschrift"
    property string bodyFont: "Bahnschrift"
    property string monoFont: "Cascadia Code"
    property string iconFont: ""
    property int pinnedCount: 0
    property var countries: []

    signal backRequested()
    signal resetLayoutRequested()
    signal clearPinnedRequested()

    readonly property var prefs: appController.settings
    readonly property var providerRows: appController.searchProviderRows
    readonly property int activeProviderCount: {
        var rows = providerRows || []
        var total = 0
        for (var i = 0; i < rows.length; i++) {
            if (rows[i].enabled && rows[i].inMenu) {
                total++
            }
        }
        return total
    }

    function setPref(key, value) {
        appController.setSetting(key, value)
    }

    function countryOptions() {
        var options = [{ value: "All", label: "All countries" }]
        for (var i = 1; i < root.countries.length; i++) {
            var country = root.countries[i]
            options.push({
                value: country.code.toUpperCase(),
                label: country.name
            })
        }
        return options
    }

    function vpnOptions() {
        var source = appController.vpnLocations || []
        var options = []
        for (var i = 0; i < source.length; i++) {
            options.push({
                value: source[i].code,
                label: source[i].name
            })
        }
        return options
    }

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
        spacing: 22

        RowLayout {
            Layout.fillWidth: true
            spacing: 16

            ColumnLayout {
                Layout.fillWidth: true
                spacing: 4

                Text {
                    text: "Control Preferences"
                    font.family: root.headingFont
                    font.pixelSize: 28
                    font.weight: Font.DemiBold
                    color: root.textPrimary
                }

                Text {
                    text: "Search network, row behaviour and engine defaults"
                    font.family: root.bodyFont
                    font.pixelSize: 14
                    color: root.textMuted
                }
            }

            AppButton {
                text: "Back to Dashboard"
                icon: "\uf060"
                iconFontFamily: root.iconFont
                kind: "ghost"
                accent: root.accent
                fontFamily: root.headingFont
                Layout.preferredHeight: 36
                Layout.preferredWidth: 168
                onClicked: root.backRequested()
            }
        }

        Flickable {
            id: settingsScroll
            objectName: "settingsScroll"
            Layout.fillWidth: true
            Layout.fillHeight: true
            contentWidth: width
            contentHeight: settingsGrid.implicitHeight
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            rightMargin: 14

            ScrollBar.vertical: AppScrollBar {
                accent: root.accent
                policy: ScrollBar.AsNeeded
                parent: settingsScroll
                x: settingsScroll.width - width
                height: settingsScroll.height
            }

            GridLayout {
                id: settingsGrid
                width: settingsScroll.width - settingsScroll.rightMargin
                columns: width >= 1080 ? 2 : 1
                columnSpacing: 20
                rowSpacing: 20

                SettingsSection {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "Location Preferences"
                    titleIcon: "\uf0ac"
                    titleIconFont: root.iconFont
                    titleFont: root.headingFont
                    titleColor: root.textPrimary
                    titleAccent: root.accent
                    panelColor: root.panelColor
                    borderColor: root.panelBorder

                    SettingRow {
                        label: "VPN provider"
                        hint: "Any VPN can be controlled manually; Mullvad supports Full Auto"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 180

                        AppSelect {
                            objectName: "vpnProviderSelect"
                            anchors.fill: parent
                            enabled: !appController.busy
                            accent: root.accent
                            textPrimary: root.textPrimary
                            textMuted: root.textMuted
                            borderStrong: root.panelBorderStrong
                            bodyFont: root.bodyFont
                            iconFont: root.iconFont
                            options: [
                                { value: "manual", label: "Manual / any VPN" },
                                { value: "mullvad", label: "Mullvad (automatic)" }
                            ]
                            value: root.prefs.vpnProvider
                            onPicked: function(provider) {
                                root.setPref("vpnProvider", provider)
                            }
                        }
                    }
                    Text {
                        Layout.fillWidth: true
                        text: root.prefs.vpnProvider === "manual"
                            ? "Manual mode: connect your VPN and route the app and browser through it before Step 1. "
                              + "Disconnect or bypass it before Steps 2 and 4. Step 3 uses saved lists only. "
                              + "Clicking a step confirms that you prepared its network. Full Auto is unavailable."
                            : "Mullvad mode: Full Auto controls Mullvad using the relay below. "
                              + "Individual steps still require you to prepare the network shown on the dashboard."
                        font.family: root.bodyFont
                        font.pixelSize: 12
                        color: root.textMuted
                        wrapMode: Text.WordWrap
                    }

                    Text {
                        Layout.fillWidth: true
                        text: "Profile location matching currently uses Chaturbate metadata. "
                            + "Other platforms compare network visibility and support manual country tags. "
                            + "A profile location is self-reported; language does not establish nationality."
                        font.family: root.bodyFont
                        font.pixelSize: 12
                        color: root.textMuted
                        wrapMode: Text.WordWrap
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "Country codes (ISO 3166-1): separate multiple codes with commas. "
                            + "Leave empty for no country preference."
                        font.family: root.bodyFont
                        font.pixelSize: 12
                        color: root.textPrimary
                        wrapMode: Text.WordWrap
                    }
                    AppTextField {
                        id: targetCountriesInput
                        objectName: "targetCountriesInput"
                        Layout.fillWidth: true
                        enabled: !appController.busy
                        accent: root.accent
                        textPrimary: root.textPrimary
                        textMuted: root.textMuted
                        bodyFont: root.bodyFont
                        text: root.prefs.targetCountries
                        placeholder: "Any country, e.g. CA, JP"
                    }
                    Text {
                        Layout.fillWidth: true
                        text: "Optional location aliases, separated by commas. Matches whole words "
                            + "in the profile location field; add spellings you want to include."
                        font.family: root.bodyFont
                        font.pixelSize: 12
                        color: root.textPrimary
                        wrapMode: Text.WordWrap
                    }
                    AppTextField {
                        id: targetLocationTermsInput
                        objectName: "targetLocationTermsInput"
                        Layout.fillWidth: true
                        enabled: !appController.busy
                        accent: root.accent
                        textPrimary: root.textPrimary
                        textMuted: root.textMuted
                        bodyFont: root.bodyFont
                        text: root.prefs.targetLocationTerms
                        placeholder: "Optional, e.g. Canada, Tokyo"
                    }
                    AppButton {
                        text: "Save location preferences"
                        enabled: !appController.busy
                        kind: "ghost"
                        accent: root.accent
                        fontFamily: root.headingFont
                        Layout.fillWidth: true
                        Layout.preferredHeight: 36
                        onClicked: {
                            locationSettingsResult.text = appController.setLocationPreferences(
                                targetCountriesInput.text, targetLocationTermsInput.text
                            ) ? "Saved. Start a new session after changing the location preferences."
                              : "Preferences were not saved. Check the codes and aliases, and wait for any active task to finish."
                        }
                    }
                    Text {
                        id: locationSettingsResult
                        Layout.fillWidth: true
                        text: "With Mullvad selected, the VPN country below selects your comparison network separately. "
                            + "Start a new session when you change location preferences."
                        font.family: root.bodyFont
                        font.pixelSize: 12
                        color: root.textMuted
                        wrapMode: Text.WordWrap
                    }
                }

                // ------------------------------------------ search network
                SettingsSection {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "Search Network"
                    titleIcon: "\uf0ac"
                    titleIconFont: root.iconFont
                    titleFont: root.headingFont
                    titleColor: root.textPrimary
                    titleAccent: root.accent
                    panelColor: root.panelColor
                    borderColor: root.panelBorder

                    headerExtra: Text {
                        text: root.activeProviderCount + " active"
                        font.family: root.monoFont
                        font.pixelSize: 11
                        color: root.textMuted
                    }

                    Text {
                        Layout.fillWidth: true
                        text: "Sources used by the magnifier popover and by "
                            + "\"Toate site-urile\". Disabled sources are skipped; "
                            + "only custom sources can be deleted."
                        font.family: root.bodyFont
                        font.pixelSize: 11
                        color: root.textMuted
                        wrapMode: Text.WordWrap
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 296
                        radius: 8
                        color: root.panelColorDeep
                        border.color: Qt.rgba(1, 1, 1, 0.06)
                        border.width: 1

                        ListView {
                            id: providerList
                            objectName: "providerList"
                            anchors.fill: parent
                            anchors.margins: 6
                            clip: true
                            spacing: 2
                            boundsBehavior: Flickable.StopAtBounds
                            model: root.providerRows

                            ScrollBar.vertical: AppScrollBar {
                                accent: root.accent
                                policy: ScrollBar.AsNeeded
                            }

                            delegate: Item {
                                id: providerRow
                                width: providerList.width - 10
                                height: 40

                                readonly property bool isEnabled: modelData.enabled
                                readonly property bool isCustom: modelData.custom
                                readonly property bool inMenu: modelData.inMenu

                                Rectangle {
                                    anchors.fill: parent
                                    radius: 6
                                    color: providerArea.containsMouse
                                        ? Qt.rgba(1, 1, 1, 0.04)
                                        : "transparent"
                                    Behavior on color {
                                        ColorAnimation { duration: 100 }
                                    }
                                }

                                MouseArea {
                                    id: providerArea
                                    anchors.fill: parent
                                    hoverEnabled: true
                                    acceptedButtons: Qt.NoButton
                                }

                                AppToggle {
                                    id: providerToggle
                                    anchors.left: parent.left
                                    anchors.leftMargin: 6
                                    anchors.verticalCenter: parent.verticalCenter
                                    text: ""
                                    accent: root.accent
                                    checked: providerRow.isEnabled
                                    onToggled: appController.setSearchProviderEnabled(
                                        modelData.id,
                                        checked
                                    )
                                }

                                Column {
                                    anchors.left: providerToggle.right
                                    anchors.leftMargin: 10
                                    anchors.right: providerActions.left
                                    anchors.rightMargin: 10
                                    anchors.verticalCenter: parent.verticalCenter
                                    spacing: 1

                                    Row {
                                        spacing: 6

                                        Text {
                                            text: modelData.label
                                            font.family: root.headingFont
                                            font.pixelSize: 13
                                            color: providerRow.isEnabled
                                                ? root.textPrimary
                                                : root.textMuted
                                        }

                                        Rectangle {
                                            visible: providerRow.isCustom
                                            anchors.verticalCenter: parent.verticalCenter
                                            width: customTag.implicitWidth + 12
                                            height: 15
                                            radius: 7
                                            color: Qt.rgba(root.accentSuccess.r,
                                                           root.accentSuccess.g,
                                                           root.accentSuccess.b, 0.14)
                                            border.color: Qt.rgba(root.accentSuccess.r,
                                                                  root.accentSuccess.g,
                                                                  root.accentSuccess.b, 0.5)
                                            border.width: 1

                                            Text {
                                                id: customTag
                                                anchors.centerIn: parent
                                                text: "CUSTOM"
                                                font.family: root.headingFont
                                                font.pixelSize: 8
                                                font.letterSpacing: 0.6
                                                color: root.accentSuccess
                                            }
                                        }

                                        Rectangle {
                                            visible: !providerRow.inMenu
                                            anchors.verticalCenter: parent.verticalCenter
                                            width: buttonTag.implicitWidth + 12
                                            height: 15
                                            radius: 7
                                            color: Qt.rgba(root.accentWarn.r,
                                                           root.accentWarn.g,
                                                           root.accentWarn.b, 0.14)
                                            border.color: Qt.rgba(root.accentWarn.r,
                                                                  root.accentWarn.g,
                                                                  root.accentWarn.b, 0.5)
                                            border.width: 1

                                            Text {
                                                id: buttonTag
                                                anchors.centerIn: parent
                                                text: "ROW BUTTON"
                                                font.family: root.headingFont
                                                font.pixelSize: 8
                                                font.letterSpacing: 0.6
                                                color: root.accentWarn
                                            }
                                        }
                                    }

                                    Text {
                                        width: parent.width
                                        text: modelData.urlTemplate
                                        font.family: root.monoFont
                                        font.pixelSize: 9
                                        color: root.textMuted
                                        opacity: 0.75
                                        elide: Text.ElideRight
                                    }
                                }

                                Row {
                                    id: providerActions
                                    anchors.right: parent.right
                                    anchors.rightMargin: 6
                                    anchors.verticalCenter: parent.verticalCenter
                                    spacing: 4

                                    IconAction {
                                        glyph: "\uf077"
                                        iconFont: root.iconFont
                                        accent: root.accent
                                        panelBorder: root.panelBorder
                                        textMuted: root.textMuted
                                        panelColorDeep: root.panelColorDeep
                                        tooltip: "Move up"
                                        active: index > 0
                                        onTriggered: appController.moveSearchProvider(
                                            modelData.id, -1
                                        )
                                    }

                                    IconAction {
                                        glyph: "\uf078"
                                        iconFont: root.iconFont
                                        accent: root.accent
                                        panelBorder: root.panelBorder
                                        textMuted: root.textMuted
                                        panelColorDeep: root.panelColorDeep
                                        tooltip: "Move down"
                                        active: index < providerList.count - 1
                                        onTriggered: appController.moveSearchProvider(
                                            modelData.id, 1
                                        )
                                    }

                                    IconAction {
                                        glyph: "\uf1f8"
                                        iconFont: root.iconFont
                                        accent: root.accentDanger
                                        panelBorder: root.panelBorder
                                        textMuted: root.textMuted
                                        panelColorDeep: root.panelColorDeep
                                        tooltip: providerRow.isCustom
                                            ? "Delete source"
                                            : "Built-in sources can only be disabled"
                                        active: providerRow.isCustom
                                        onTriggered: appController.removeSearchProvider(
                                            modelData.id
                                        )
                                    }
                                }
                            }
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 1
                        color: root.panelBorder
                        opacity: 0.4
                    }

                    Text {
                        Layout.fillWidth: true
                        text: "ADD A SOURCE"
                        font.family: root.headingFont
                        font.pixelSize: 10
                        font.letterSpacing: 1.2
                        color: root.textMuted
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        AppTextField {
                            id: providerLabelInput
                            Layout.preferredWidth: 150
                            Layout.preferredHeight: 34
                            accent: root.accent
                            textPrimary: root.textPrimary
                            textMuted: root.textMuted
                            bodyFont: root.bodyFont
                            placeholder: "Name"
                            onAccepted: addProviderButton.clicked()
                        }

                        AppTextField {
                            id: providerTemplateInput
                            Layout.fillWidth: true
                            Layout.preferredHeight: 34
                            accent: root.accent
                            textPrimary: root.textPrimary
                            textMuted: root.textMuted
                            bodyFont: root.bodyFont
                            placeholder: "https://site.tv/search/{query}"
                            onAccepted: addProviderButton.clicked()
                        }

                        AppButton {
                            id: addProviderButton
                            text: "Add"
                            icon: "\uf067"
                            iconFontFamily: root.iconFont
                            kind: "primary"
                            accent: root.accent
                            fontFamily: root.headingFont
                            compact: true
                            Layout.preferredWidth: 96
                            Layout.preferredHeight: 34
                            onClicked: {
                                var error = appController.addSearchProvider(
                                    providerLabelInput.text,
                                    providerTemplateInput.text
                                )
                                providerError.text = error
                                if (error === "") {
                                    providerLabelInput.text = ""
                                    providerTemplateInput.text = ""
                                }
                            }
                        }
                    }

                    Text {
                        id: providerError
                        Layout.fillWidth: true
                        visible: text !== ""
                        text: ""
                        font.family: root.bodyFont
                        font.pixelSize: 11
                        color: root.accentDanger
                        wrapMode: Text.WordWrap
                    }

                    AppButton {
                        text: "Restore default sources"
                        icon: "\uf021"
                        iconFontFamily: root.iconFont
                        kind: "ghost"
                        accent: root.accent
                        fontFamily: root.headingFont
                        compact: true
                        Layout.preferredWidth: 210
                        Layout.preferredHeight: 32
                        onClicked: appController.resetSearchProviders()
                    }
                }

                // --------------------------------------------- model rows
                SettingsSection {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "Model Rows"
                    titleIcon: "\uf022"
                    titleIconFont: root.iconFont
                    titleFont: root.headingFont
                    titleColor: root.textPrimary
                    titleAccent: root.accent
                    panelColor: root.panelColor
                    borderColor: root.panelBorder

                    SettingRow {
                        label: "Copy the model when opening its page"
                        hint: "Clicking a row copies the handle to the clipboard"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 60

                        AppToggle {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: ""
                            accent: root.accent
                            checked: root.prefs.copyOnOpen
                            onToggled: root.setPref("copyOnOpen", checked)
                        }
                    }

                    SettingRow {
                        label: "What gets copied"
                        hint: "Handle only, or the full model URL"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 170

                        AppSelect {
                            anchors.fill: parent
                            accent: root.accent
                            textPrimary: root.textPrimary
                            textMuted: root.textMuted
                            borderStrong: root.panelBorderStrong
                            bodyFont: root.bodyFont
                            iconFont: root.iconFont
                            enabled: root.prefs.copyOnOpen
                            opacity: enabled ? 1.0 : 0.45
                            options: [
                                { value: "username", label: "Username" },
                                { value: "url", label: "Full URL" }
                            ]
                            value: root.prefs.copyMode
                            onPicked: function(mode) {
                                root.setPref("copyMode", mode)
                            }
                        }
                    }

                    SettingRow {
                        label: "Open model pages in"
                        hint: "External searches always use Chrome"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 170

                        AppSelect {
                            anchors.fill: parent
                            accent: root.accent
                            textPrimary: root.textPrimary
                            textMuted: root.textMuted
                            borderStrong: root.panelBorderStrong
                            bodyFont: root.bodyFont
                            iconFont: root.iconFont
                            options: [
                                { value: "default", label: "Default browser" },
                                { value: "chrome", label: "Chrome" }
                            ]
                            value: root.prefs.openModelIn
                            onPicked: function(target) {
                                root.setPref("openModelIn", target)
                            }
                        }
                    }

                    SettingRow {
                        label: "Models per batch"
                        hint: "Used by \"Open next batch\" on the dashboard"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 150

                        AppStepper {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            accent: root.accent
                            textPrimary: root.textPrimary
                            textMuted: root.textMuted
                            panelBorder: root.panelBorder
                            headingFont: root.headingFont
                            minimum: 1
                            maximum: 25
                            value: root.prefs.openBatchSize
                            onChanged: function(next) {
                                root.setPref("openBatchSize", next)
                            }
                        }
                    }

                    SettingRow {
                        label: "Ask before deleting"
                        hint: "Turn off to delete straight from the trash icon"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 60

                        AppToggle {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: ""
                            accent: root.accent
                            checked: root.prefs.confirmDelete
                            onToggled: root.setPref("confirmDelete", checked)
                        }
                    }

                    SettingRow {
                        label: "Blacklist on delete by default"
                        hint: "Pre-checks \"Block permanently\" in the dialog"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 60

                        AppToggle {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: ""
                            accent: root.accentDanger
                            checked: root.prefs.blockOnDelete
                            onToggled: root.setPref("blockOnDelete", checked)
                        }
                    }
                }

                // ----------------------------------------- engine & paths
                SettingsSection {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "Engine & Paths"
                    titleIcon: "\uf0e7"
                    titleIconFont: root.iconFont
                    titleFont: root.headingFont
                    titleColor: root.textPrimary
                    titleAccent: root.accentAlt
                    panelColor: root.panelColor
                    borderColor: root.panelBorder

                    SettingRow {
                        label: "Hide browser (steps 1-2)"
                        hint: "Default for the dashboard toggle"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 60

                        AppToggle {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: ""
                            accent: root.accent
                            checked: root.prefs.hideBrowserSteps12
                            onToggled: root.setPref("hideBrowserSteps12", checked)
                        }
                    }

                    SettingRow {
                        label: "Hide browser except captcha (step 4)"
                        hint: "Default for the dashboard toggle"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 60

                        AppToggle {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: ""
                            accent: root.accent
                            checked: root.prefs.hideBrowserStep4
                            onToggled: root.setPref("hideBrowserStep4", checked)
                        }
                    }

                    SettingRow {
                        label: "Mullvad country for step 1"
                        hint: "For manual mode, choose the country in your own VPN app"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.accentWarn
                        controlWidth: 170

                        AppSelect {
                            anchors.fill: parent
                            accent: root.accentAlt
                            textPrimary: root.textPrimary
                            textMuted: root.textMuted
                            borderStrong: root.panelBorderStrong
                            bodyFont: root.bodyFont
                            iconFont: root.iconFont
                            enabled: !appController.busy && root.prefs.vpnProvider === "mullvad"
                            options: root.vpnOptions()
                            value: root.prefs.vpnRelayLocation
                            onPicked: function(code) {
                                root.setPref("vpnRelayLocation", code)
                            }
                        }
                    }

                    SettingRow {
                        label: "Default platform at startup"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 170

                        AppSelect {
                            anchors.fill: parent
                            accent: root.accent
                            textPrimary: root.textPrimary
                            textMuted: root.textMuted
                            borderStrong: root.panelBorderStrong
                            bodyFont: root.bodyFont
                            iconFont: root.iconFont
                            options: [
                                { value: "Chaturbate", label: "Chaturbate" },
                                { value: "MyFreeCams", label: "MyFreeCams" },
                                { value: "Stripchat", label: "Stripchat" },
                                { value: "XHamsterLive", label: "XHamsterLive" }
                            ]
                            value: root.prefs.defaultPlatform
                            onPicked: function(platform) {
                                root.setPref("defaultPlatform", platform)
                            }
                        }
                    }

                    SettingRow {
                        label: "Load the latest session at startup"
                        hint: "Skips clicking \"Use Latest Session\" every launch"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 60

                        AppToggle {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: ""
                            accent: root.accent
                            checked: root.prefs.autoLoadLatestSession
                            onToggled: root.setPref(
                                "autoLoadLatestSession", checked
                            )
                        }
                    }

                    SettingRow {
                        label: "Chrome executable"
                        hint: "Empty = auto-detect (PATH, env var, default install)"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 260

                        RowLayout {
                            anchors.fill: parent
                            spacing: 6

                            AppTextField {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 34
                                accent: root.accent
                                textPrimary: root.textPrimary
                                textMuted: root.textMuted
                                bodyFont: root.bodyFont
                                placeholder: "chrome.exe"
                                text: root.prefs.chromePath
                                onEdited: root.setPref("chromePath", text)
                                onAccepted: root.setPref("chromePath", text)
                            }

                            AppButton {
                                text: ""
                                icon: "\uf07c"
                                iconFontFamily: root.iconFont
                                kind: "ghost"
                                accent: root.accent
                                fontFamily: root.headingFont
                                compact: true
                                Layout.preferredWidth: 38
                                Layout.preferredHeight: 34
                                onClicked: appController.pickExecutablePath(
                                    "chromePath"
                                )
                            }
                        }
                    }

                    SettingRow {
                        label: "Mullvad CLI executable"
                        hint: "Empty = auto-detect (PATH, env var, default install)"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 260

                        RowLayout {
                            anchors.fill: parent
                            spacing: 6

                            AppTextField {
                                Layout.fillWidth: true
                                Layout.preferredHeight: 34
                                accent: root.accent
                                textPrimary: root.textPrimary
                                textMuted: root.textMuted
                                bodyFont: root.bodyFont
                                placeholder: "mullvad.exe"
                                text: root.prefs.mullvadPath
                                onEdited: root.setPref("mullvadPath", text)
                                onAccepted: root.setPref("mullvadPath", text)
                            }

                            AppButton {
                                text: ""
                                icon: "\uf07c"
                                iconFontFamily: root.iconFont
                                kind: "ghost"
                                accent: root.accent
                                fontFamily: root.headingFont
                                compact: true
                                Layout.preferredWidth: 38
                                Layout.preferredHeight: 34
                                onClicked: appController.pickExecutablePath(
                                    "mullvadPath"
                                )
                            }
                        }
                    }
                }

                // ---------------------------------- master list & interface
                SettingsSection {
                    Layout.fillWidth: true
                    Layout.alignment: Qt.AlignTop
                    title: "Master List & Interface"
                    titleIcon: "\uf2d0"
                    titleIconFont: root.iconFont
                    titleFont: root.headingFont
                    titleColor: root.textPrimary
                    titleAccent: root.accent
                    panelColor: root.panelColor
                    borderColor: root.panelBorder

                    SettingRow {
                        label: "Remember sort and country filter"
                        hint: "Restores the Master List view on the next launch"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 60

                        AppToggle {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: ""
                            accent: root.accent
                            checked: root.prefs.rememberMasterListView
                            onToggled: root.setPref(
                                "rememberMasterListView", checked
                            )
                        }
                    }

                    SettingRow {
                        label: "Startup country filter"
                        hint: "Applied when the view above is remembered"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 170

                        AppSelect {
                            anchors.fill: parent
                            accent: root.accent
                            textPrimary: root.textPrimary
                            textMuted: root.textMuted
                            borderStrong: root.panelBorderStrong
                            bodyFont: root.bodyFont
                            iconFont: root.iconFont
                            enabled: root.prefs.rememberMasterListView
                            opacity: enabled ? 1.0 : 0.45
                            options: root.countryOptions()
                            value: root.prefs.masterListCountryFilter
                            onPicked: function(code) {
                                appController.setMasterListCountryFilter(code)
                            }
                        }
                    }

                    SettingRow {
                        label: "Remember window size and position"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 60

                        AppToggle {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: ""
                            accent: root.accent
                            checked: root.prefs.rememberWindowGeometry
                            onToggled: root.setPref(
                                "rememberWindowGeometry", checked
                            )
                        }
                    }

                    SettingRow {
                        label: "Follow the activity log"
                        hint: "Keeps scrolling to the newest line"
                        labelFont: root.bodyFont
                        hintFont: root.bodyFont
                        labelColor: root.textPrimary
                        hintColor: root.textMuted
                        controlWidth: 60

                        AppToggle {
                            anchors.right: parent.right
                            anchors.verticalCenter: parent.verticalCenter
                            text: ""
                            accent: root.accent
                            checked: root.prefs.logAutoScroll
                            onToggled: root.setPref("logAutoScroll", checked)
                        }
                    }

                    Rectangle {
                        Layout.fillWidth: true
                        Layout.preferredHeight: 1
                        color: root.panelBorder
                        opacity: 0.4
                    }

                    RowLayout {
                        Layout.fillWidth: true
                        spacing: 8

                        AppButton {
                            text: "Reset panel layout"
                            icon: "\uf2d2"
                            iconFontFamily: root.iconFont
                            kind: "ghost"
                            accent: root.accent
                            fontFamily: root.headingFont
                            compact: true
                            Layout.fillWidth: true
                            Layout.preferredHeight: 32
                            onClicked: root.resetLayoutRequested()
                        }

                        AppButton {
                            text: "Clear pinned (" + root.pinnedCount + ")"
                            icon: "\uf08d"
                            iconFontFamily: root.iconFont
                            kind: "ghost"
                            accent: root.accent
                            fontFamily: root.headingFont
                            compact: true
                            enabled: root.pinnedCount > 0
                            Layout.fillWidth: true
                            Layout.preferredHeight: 32
                            onClicked: root.clearPinnedRequested()
                        }
                    }

                    AppButton {
                        text: "Restore all preferences to defaults"
                        icon: "\uf021"
                        iconFontFamily: root.iconFont
                        kind: "ghost"
                        accent: root.accentDanger
                        fontFamily: root.headingFont
                        compact: true
                        Layout.fillWidth: true
                        Layout.preferredHeight: 32
                        onClicked: appController.resetPreferences()
                    }

                    Text {
                        Layout.fillWidth: true
                        text: appController.settingsPath
                        font.family: root.monoFont
                        font.pixelSize: 9
                        color: root.textMuted
                        opacity: 0.7
                        elide: Text.ElideMiddle
                    }
                }
            }
        }
    }
}
