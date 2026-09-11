import QtQuick 2.15

Item {
    id: root
    property color panelColor: "#061521"
    property color borderColor: "#0f3b4f"
    property color accentColor: "#12d6ff"
    property real accentOpacity: 0.4
    property real shadowOpacity: 0.4
    property int shadowOffset: 6
    property int radius: 10
    property int padding: 16
    // Optional consistent header
    property string title: ""
    property string titleIcon: ""
    property string titleIconFont: ""
    property string titleFont: "Bahnschrift"
    property color titleColor: "#eef8ff"
    property color titleAccent: accentColor
    default property alias content: contentItem.data
    property alias headerExtra: headerExtraItem.data

    implicitWidth: 280
    implicitHeight: 200

    Rectangle {
        anchors.fill: parent
        anchors.topMargin: root.shadowOffset
        anchors.bottomMargin: -root.shadowOffset
        radius: root.radius
        color: "#00040a"
        opacity: root.shadowOpacity
    }

    Rectangle {
        anchors.fill: parent
        radius: root.radius
        gradient: Gradient {
            GradientStop { position: 0.0; color: Qt.lighter(root.panelColor, 1.12) }
            GradientStop { position: 1.0; color: Qt.darker(root.panelColor, 1.16) }
        }
        border.color: root.borderColor
        border.width: 1
    }

    // Top accent hairline
    Rectangle {
        height: 1
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: root.radius
        anchors.rightMargin: root.radius
        color: root.accentColor
        opacity: root.accentOpacity
    }

    Item {
        id: headerRow
        visible: root.title !== ""
        anchors.left: parent.left
        anchors.right: parent.right
        anchors.top: parent.top
        anchors.leftMargin: root.padding
        anchors.rightMargin: root.padding
        anchors.topMargin: root.title !== "" ? 14 : 0
        height: root.title !== "" ? 24 : 0

        Rectangle {
            id: headerTick
            width: 3
            height: 14
            radius: 1.5
            anchors.verticalCenter: parent.verticalCenter
            color: root.titleAccent
            opacity: 0.9
        }

        Text {
            id: headerIconText
            visible: root.titleIcon !== ""
            anchors.left: headerTick.right
            anchors.leftMargin: 9
            anchors.verticalCenter: parent.verticalCenter
            text: root.titleIcon
            font.family: root.titleIconFont
            font.pixelSize: 11
            color: root.titleAccent
            opacity: 0.85
        }

        Text {
            anchors.left: headerIconText.visible ? headerIconText.right : headerTick.right
            anchors.leftMargin: headerIconText.visible ? 7 : 9
            anchors.right: headerExtraItem.left
            anchors.rightMargin: 8
            anchors.verticalCenter: parent.verticalCenter
            text: root.title.toUpperCase()
            font.family: root.titleFont
            font.pixelSize: 12
            font.letterSpacing: 1.6
            color: root.titleColor
            elide: Text.ElideRight
        }

        Item {
            id: headerExtraItem
            anchors.right: parent.right
            anchors.verticalCenter: parent.verticalCenter
            width: childrenRect.width
            height: parent.height
        }
    }

    Item {
        id: contentItem
        anchors.fill: parent
        anchors.margins: root.padding
        anchors.topMargin: root.title !== ""
            ? root.padding + headerRow.height + 8
            : root.padding
    }
}
