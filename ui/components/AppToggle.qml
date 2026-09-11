import QtQuick 2.15
import QtQuick.Controls 2.15

CheckBox {
    id: control
    property color accent: "#12d6ff"
    property string fontFamily: "Bahnschrift"
    spacing: 10
    leftPadding: indicator.implicitWidth + spacing
    rightPadding: 2
    topPadding: 2
    bottomPadding: 2
    implicitHeight: Math.max(indicator.implicitHeight, contentItem.implicitHeight) + topPadding + bottomPadding
    implicitWidth: leftPadding + contentItem.implicitWidth + rightPadding

    indicator: Rectangle {
        implicitWidth: 40
        implicitHeight: 21
        radius: 10.5
        color: control.checked
            ? Qt.rgba(control.accent.r, control.accent.g, control.accent.b, 0.20)
            : "#071622"
        border.color: control.checked
            ? control.accent
            : (control.hovered ? "#2a5a70" : "#174052")
        border.width: 1
        Behavior on color { ColorAnimation { duration: 140 } }
        Behavior on border.color { ColorAnimation { duration: 140 } }

        Rectangle {
            width: 15
            height: 15
            radius: 7.5
            y: 3
            x: control.checked ? parent.width - width - 3 : 3
            color: control.checked ? control.accent : "#6f8497"
            Behavior on x { NumberAnimation { duration: 140; easing.type: Easing.OutCubic } }
            Behavior on color { ColorAnimation { duration: 140 } }
        }
    }

    contentItem: Text {
        text: control.text
        font.family: control.fontFamily
        font.pixelSize: 12
        font.letterSpacing: 0.2
        color: control.hovered ? "#eef8ff" : "#d8e9f4"
        verticalAlignment: Text.AlignVCenter
    }
}
