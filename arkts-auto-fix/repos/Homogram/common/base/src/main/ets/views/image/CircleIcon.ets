@Component
export struct CircleButton {
  private icon: Resource = $r('sys.symbol.more')

  build() {
    Button({ type: ButtonType.Circle, stateEffect: true }) {
      Text() {
        SymbolSpan(this.icon)
      }
      .fontSize(26)
    }
    .backgroundBlurStyle(BlurStyle.BACKGROUND_THIN)
  }
}