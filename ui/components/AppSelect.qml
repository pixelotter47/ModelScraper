import QtQuick 2.15
import QtQuick.Controls 2.15

// Styled ComboBox over an [{ value, label }] option list.
ComboBox {
    id: control

    property var options: []
    property string value: ""
    property color accent: "#12d6ff"
    property color textPrimary: "#eef8ff"
    property color textMuted: "#7f98aa"
    property color borderStrong: "#1f6b88"
    property string bodyFont: "Bahnschrift"
    property string iconFont: ""

    signal picked(string value)

    function indexOfValue(target) {
        for (var i = 0; i < options.length; i++) {
            if (options[i].value === target) {
                return i
            }
        }
        return -1
    }

    model: options
    currentIndex: indexOfValue(value)
    displayText: currentIndex >= 0 ? options[currentIndex].label : ""
    implicitHeight: 34

    onActivated: function(index) {
        if (index >= 0 && index < options.length) {
            control.picked(options[index].value)
        }
    }

    indicator: Text {
        x: control.width - width - 12
        y: (control.height - height) / 2
        text: "\uf078"
        font.family: control.iconFont
        font.pixelSize: 9
        color: control.pressed || control.hovered
            ? control.accent
            : control.textMuted
    }

    delegate: ItemDelegate {
        width: control.width - 8
        height: 32
        contentItem: Text {
            text: modelData.label
            color: highlighted ? control.textPrimary : control.textMuted
            font.family: control.bodyFont
            font.pixelSize: 13
            verticalAlignment: Text.AlignVCenter
            elide: Text.ElideRight
        }
        background: Rectangle {
            radius: 6
            color: highlighted
                ? Qt.rgba(control.accent.r, control.accent.g,
                          control.accent.b, 0.12)
                : "transparent"
        }
    }

    popup: Popup {
        y: control.height + 4
        width: control.width
        implicitHeight: Math.min(contentItem.implicitHeight + 8, 260)
        padding: 4

        contentItem: ListView {
            clip: true
            implicitHeight: contentHeight
            model: control.popup.visible ? control.delegateModel : null
            currentIndex: control.highlightedIndex
            ScrollBar.vertical: AppScrollBar { accent: control.accent }
        }

        background: Rectangle {
            color: "#050f18"
            border.color: control.borderStrong
            border.width: 1
            radius: 10
        }
    }

    contentItem: Text {
        leftPadding: 12
        rightPadding: 28
        text: control.displayText
        font.family: control.bodyFont
        font.pixelSize: 13
        color: control.textPrimary
        verticalAlignment: Text.AlignVCenter
        elide: Text.ElideRight
    }

    background: Rectangle {
        color: control.hovered ? "#071c2b" : "#03101a"
        border.color: control.popup.visible
            ? Qt.rgba(control.accent.r, control.accent.g, control.accent.b, 0.7)
            : Qt.rgba(1, 1, 1, 0.08)
        border.width: 1
        radius: 8
        Behavior on color { ColorAnimation { duration: 120 } }
    }
}
