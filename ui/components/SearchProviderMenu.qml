import QtQuick 2.15
import QtQuick.Controls 2.15

Popup {
    id: root
    objectName: "searchProviderMenu"

    property Item popupHost
    property var providers: []
    property string modelName: ""
    property string headingFont: ""
    property string bodyFont: ""
    property string iconFont: ""
    property color accent: "#12d6ff"
    property color panelColor: "#061521"
    property color panelColorDeep: "#03101a"
    property color panelBorder: "#12394a"
    property color panelBorderStrong: "#1f6b88"
    property color textPrimary: "#eef8ff"
    property color textMuted: "#7f98aa"

    signal providerSelected(string providerId, string modelName)
    signal allProvidersSelected(string modelName)

    parent: popupHost
    width: 276
    height: 462
    padding: 0
    modal: false
    focus: true
    closePolicy: Popup.CloseOnEscape | Popup.CloseOnPressOutside

    enter: Transition {
        ParallelAnimation {
            NumberAnimation {
                property: "opacity"
                from: 0
                to: 1
                duration: 130
                easing.type: Easing.OutCubic
            }
            NumberAnimation {
                property: "scale"
                from: 0.96
                to: 1
                duration: 150
                easing.type: Easing.OutBack
            }
        }
    }

    exit: Transition {
        ParallelAnimation {
            NumberAnimation {
                property: "opacity"
                from: 1
                to: 0
                duration: 90
            }
            NumberAnimation {
                property: "scale"
                from: 1
                to: 0.98
                duration: 90
            }
        }
    }

    function openFor(anchorItem, username) {
        modelName = String(username || "").trim()
        if (!anchorItem || modelName.length === 0 || !root.parent) {
            return
        }

        var point = anchorItem.mapToItem(root.parent, 0, anchorItem.height)
        x = Math.max(
            10,
            Math.min(
                point.x + anchorItem.width - width,
                root.parent.width - width - 10
            )
        )

        var below = point.y + 6
        var above = point.y - anchorItem.height - height - 6
        y = below + height <= root.parent.height - 10
            ? below
            : Math.max(10, above)
        open()
    }

    background: Rectangle {
        radius: 12
        color: root.panelColor
        border.color: root.accent
        border.width: 1

        Rectangle {
            anchors.fill: parent
            anchors.margins: 1
            radius: 11
            color: "transparent"
            border.color: "#1f6b88"
            border.width: 1
            opacity: 0.55
        }

        Rectangle {
            anchors.left: parent.left
            anchors.right: parent.right
            anchors.top: parent.top
            height: 3
            radius: 12
            gradient: Gradient {
                orientation: Gradient.Horizontal
                GradientStop { position: 0.0; color: root.accent }
                GradientStop { position: 1.0; color: "#39e75f" }
            }
        }
    }

    contentItem: Column {
        spacing: 0

        Item {
            width: parent.width
            height: 62

            Column {
                anchors.left: parent.left
                anchors.leftMargin: 14
                anchors.right: closeButton.left
                anchors.rightMargin: 8
                anchors.verticalCenter: parent.verticalCenter
                spacing: 1

                Text {
                    width: parent.width
                    text: "SEARCH NETWORK"
                    color: root.accent
                    font.family: root.headingFont
                    font.pixelSize: 12
                    font.bold: true
                    font.letterSpacing: 1.4
                }

                Text {
                    width: parent.width
                    text: root.modelName
                    color: root.textPrimary
                    font.family: root.bodyFont
                    font.pixelSize: 13
                    elide: Text.ElideRight
                }
            }

            Rectangle {
                id: closeButton
                anchors.right: parent.right
                anchors.rightMargin: 10
                anchors.verticalCenter: parent.verticalCenter
                width: 26
                height: 26
                radius: 7
                color: closeArea.containsMouse ? "#11354a" : root.panelColorDeep
                border.color: closeArea.containsMouse
                    ? root.accent
                    : root.panelBorder

                Text {
                    anchors.centerIn: parent
                    text: "\uf00d"
                    font.family: root.iconFont
                    font.pixelSize: 11
                    color: closeArea.containsMouse
                        ? root.accent
                        : root.textMuted
                }

                MouseArea {
                    id: closeArea
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: root.close()
                }
            }
        }

        Rectangle {
            width: parent.width - 20
            height: 45
            anchors.horizontalCenter: parent.horizontalCenter
            radius: 8
            color: allArea.containsMouse ? "#103f43" : "#08262d"
            border.color: allArea.containsMouse ? "#39e75f" : "#1c6264"
            border.width: 1

            Behavior on color { ColorAnimation { duration: 100 } }

            Row {
                anchors.fill: parent
                anchors.leftMargin: 12
                anchors.rightMargin: 10
                spacing: 10

                Rectangle {
                    anchors.verticalCenter: parent.verticalCenter
                    width: 24
                    height: 24
                    radius: 7
                    color: "#163f3c"

                    Text {
                        anchors.centerIn: parent
                        text: "\uf0ac"
                        font.family: root.iconFont
                        font.pixelSize: 11
                        color: "#58f59a"
                    }
                }

                Column {
                    anchors.verticalCenter: parent.verticalCenter
                    width: parent.width - 76
                    spacing: 0

                    Text {
                        text: "TOATE SITE-URILE"
                        color: "#d9ffe8"
                        font.family: root.headingFont
                        font.pixelSize: 12
                        font.bold: true
                        font.letterSpacing: 0.7
                    }

                    Text {
                        text: root.providers.length + " căutări simultane"
                        color: "#72b99a"
                        font.family: root.bodyFont
                        font.pixelSize: 10
                    }
                }

                Text {
                    anchors.verticalCenter: parent.verticalCenter
                    text: "\uf054"
                    font.family: root.iconFont
                    font.pixelSize: 10
                    color: allArea.containsMouse ? "#58f59a" : "#488475"
                }
            }

            MouseArea {
                id: allArea
                anchors.fill: parent
                hoverEnabled: true
                cursorShape: Qt.PointingHandCursor
                onClicked: {
                    root.allProvidersSelected(root.modelName)
                    root.close()
                }
            }
        }

        Item {
            width: parent.width
            height: 34

            Text {
                anchors.left: parent.left
                anchors.leftMargin: 14
                anchors.verticalCenter: parent.verticalCenter
                text: "ALEGE O SURSĂ"
                color: root.textMuted
                font.family: root.headingFont
                font.pixelSize: 10
                font.bold: true
                font.letterSpacing: 1.2
            }

            Rectangle {
                anchors.left: parent.left
                anchors.leftMargin: 112
                anchors.right: parent.right
                anchors.rightMargin: 12
                anchors.verticalCenter: parent.verticalCenter
                height: 1
                color: root.panelBorder
            }
        }

        ListView {
            id: providerList
            width: parent.width
            height: parent.height - 141
            clip: true
            boundsBehavior: Flickable.StopAtBounds
            model: root.providers
            ScrollBar.vertical: ScrollBar {
                policy: ScrollBar.AsNeeded
                width: 7
            }

            delegate: Item {
                id: providerRow
                width: providerList.width
                height: 34

                Rectangle {
                    anchors.fill: parent
                    anchors.leftMargin: 8
                    anchors.rightMargin: 8
                    radius: 7
                    color: providerArea.containsMouse ? "#0b3445" : "transparent"
                    border.color: providerArea.containsMouse
                        ? root.panelBorderStrong
                        : "transparent"
                    border.width: 1

                    Behavior on color { ColorAnimation { duration: 90 } }
                }

                Rectangle {
                    anchors.left: parent.left
                    anchors.leftMargin: 13
                    anchors.verticalCenter: parent.verticalCenter
                    width: 5
                    height: 5
                    radius: 3
                    color: index === 0 ? "#39e75f" : root.accent
                    opacity: providerArea.containsMouse ? 1 : 0.55
                }

                Text {
                    anchors.left: parent.left
                    anchors.leftMargin: 28
                    anchors.right: arrow.left
                    anchors.rightMargin: 8
                    anchors.verticalCenter: parent.verticalCenter
                    text: modelData.label
                    color: providerArea.containsMouse
                        ? root.textPrimary
                        : (index === 0 ? "#baf9cf" : root.textMuted)
                    font.family: root.bodyFont
                    font.pixelSize: 13
                    font.bold: index === 0
                    elide: Text.ElideRight
                }

                Text {
                    id: arrow
                    anchors.right: parent.right
                    anchors.rightMargin: 18
                    anchors.verticalCenter: parent.verticalCenter
                    text: "\uf35d"
                    font.family: root.iconFont
                    font.pixelSize: 10
                    color: providerArea.containsMouse
                        ? root.accent
                        : root.panelBorderStrong
                }

                MouseArea {
                    id: providerArea
                    anchors.fill: parent
                    hoverEnabled: true
                    cursorShape: Qt.PointingHandCursor
                    onClicked: {
                        root.providerSelected(modelData.id, root.modelName)
                        root.close()
                    }
                }
            }
        }
    }
}
