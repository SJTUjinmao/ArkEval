import {
  PhotoPickerComponent,
  PickerController,
  PickerOptions,
  DataType,
  ItemInfo,
  ItemType,
  PhotoBrowserInfo,
  ClickType,
  PickerColorMode,
  MaxSelected,
  MaxCountType,
  ReminderMode,
  PickerOrientation,
  BaseItemInfo,
  PhotoBrowserRange,
  SelectMode,
} from '@ohos.file.PhotoPickerComponent';
import photoAccessHelper from '@ohos.file.photoAccessHelper';

@Component
export struct ChatDetailBottomPhotoPicker {
  // 组件初始化时设置参数信息
  pickerOptions: PickerOptions = new PickerOptions();
  // 组件初始化完成后，可控制组件部分行为
  @State pickerController: PickerController = new PickerController();
  @State selectedUris: string[] = [];
  @State currentUri: string = '';
  @State isBlock: boolean = false; // whether to allow opening system camera
  @Link selectedMediaCount: number;
  @Link animate: boolean;

  build() {
    PhotoPickerComponent({
      // 设置组件选择选项实例
      pickerOptions: this.pickerOptions,

      // 资源被选中回调，返回url信息（返回的uri权限是只读权限）
      onSelect: (uri: string): void => this.onSelect(uri),

      // 资源被反选回调，返回url信息（返回的uri权限是只读权限）
      onDeselect: (uri: string): void => this.onDeselect(uri),

      /**
       * 资源被选中回调，返回资源的信息，以及选中方式
       * ItemInfo（itemType, uri, mimeType, width, height, size, duration）
       * ClickType（SELECTED：勾选， DESELECTED： 反选）
       * return 返回值为true时才会给url授权（返回的uri权限是只读权限）
       */
      onItemClicked: (itemInfo: ItemInfo, clickType: ClickType): boolean => this.onItemClicked(itemInfo, clickType),

      /**
       * 进入大图页回调,
       * PhotoBrowserInfo (animatorParams: 动效)
       */
      onEnterPhotoBrowser: (photoBrowserInfo: PhotoBrowserInfo): boolean => this.onEnterPhotoBrowser(photoBrowserInfo),

      /**
       * 退出大图页回调,
       * PhotoBrowserInfo (animatorParams: 动效)
       */
      onExitPhotoBrowser: (photoBrowserInfo: PhotoBrowserInfo): boolean => this.onExitPhotoBrowser(photoBrowserInfo),

      /**
       * 切换大图页内容回调事件,
       * PhotoBrowserInfo (animatorParams: 动效)
       */
      onPhotoBrowserChanged: (photoBrowserItemInfo: BaseItemInfo): boolean => this.onPhotoBrowserChanged(photoBrowserItemInfo),

      // 接收到该回调后，便可通过pickerController相关接口向picker发送数据，在此之前不生效。
      onPickerControllerReady: (): void => this.onPickerControllerReady(),

      // picker控制实例，应用可通过PickerController向picker组件发送数据，实现控制PhotoPickerComponent组件行为
      pickerController: this.pickerController,
    })
  }

  aboutToAppear() {
    // 设置picker宫格页数据类型
    this.pickerOptions.MIMEType = photoAccessHelper.PhotoViewMIMETypes.IMAGE_TYPE;

    // 是否展示搜索框，默认false
    this.pickerOptions.isSearchSupported = true;

    // 是否支持拍照，默认false
    this.pickerOptions.isPhotoTakingSupported = true;

    // 是否支持重复选择
    this.pickerOptions.isRepeatSelectSupported = false;

    // 最大选择数量
    this.pickerOptions.maxSelectNumber = 8; // 最大选择数量

    // 最大图片选择数量
    this.pickerOptions.maxPhotoSelectNumber = 8; // 最大图片选择数量

    // 最大视频选择数量
    this.pickerOptions.maxVideoSelectNumber = 8; // 最大视频选择数量

    // 超出最大选择数量时
    this.pickerOptions.maxSelectedReminderMode = ReminderMode.TOAST;

    // 设置大图背景色
    this.pickerOptions.photoBrowserBackgroundColorMode = PickerColorMode.AUTO;

    // 设置checkbox选中颜色
    this.pickerOptions.checkBoxColor = '#000000';

    // 设置checkbox文字颜色
    this.pickerOptions.checkboxTextColor = '#000000';

    // 设置宫格页背景色
    this.pickerOptions.backgroundColor = `#F6F6F6`;

    // 设置选择模式，默认：MULTI_SELECT
    this.pickerOptions.selectMode = SelectMode.MULTI_SELECT;
  }

  // 进入大图页回调
  private onEnterPhotoBrowser(photoBrowserInfo: PhotoBrowserInfo): boolean {
    console.info('onEnterPhotoBrowser' + JSON.stringify(photoBrowserInfo));
    return false;
  }

  // 退出大图页回调
  private onExitPhotoBrowser(photoBrowserInfo: PhotoBrowserInfo): boolean {
    console.info('onExitPhotoBrowser' + JSON.stringify(photoBrowserInfo));
    return false;
  }

  // 接收到该回调后，便可通过pickerController相关接口向picker发送数据，在此之前不生效。
  private onPickerControllerReady(): void {
    console.info('onPickerControllerReady');
  }

  // 大图页图片切换回调事件处理
  private onPhotoBrowserChanged(photoBrowserItemInfo: BaseItemInfo): boolean {
    console.info('onPhotoBrowserChanged' + JSON.stringify(photoBrowserItemInfo));
    this.currentUri = photoBrowserItemInfo.uri ?? '';
    return true;
  }

  // 图片/视频被选中的回调，返回的url只有只读权限，不能用url直接去打开
  private onSelect(uri: string): void {
    if (uri) {
      this.selectedUris.push(uri)
    }
    // 将uri保存起来，并保证切换不同的宫格页组件时保持选中项一致
    this.pickerOptions.preselectedUris = [...this.selectedUris];
    this.selectedMediaCount = this.selectedUris.length;
    console.info('onSelect' + JSON.stringify(this.selectedUris));
  }

  // 图片/视频被取消勾选的回调，返回的url只有只读权限，不能用url直接去打开
  private onDeselect(uri: string): void {
    if (uri) {
      this.selectedUris = this.selectedUris.filter((item: string) => {
        return item !== uri;
      })
    }
    // 排除反勾选uri，并保证切换不同的宫格页组件时保持选中项一致
    this.pickerOptions.preselectedUris = [...this.selectedUris];
    this.selectedMediaCount = this.selectedUris.length;
    console.info('onDeselect' + JSON.stringify(this.selectedUris));
  }

  /**
   * 图片/视频被选中回调，返回资源的信息，以及选中方式
   * ItemInfo 图片、视频相关信息
   * ClickType（SELECTED：勾选， DESELECTED： 反选）
   * return 返回值为true时才会给url授权（返回的uri权限是只读权限）
   */
  private onItemClicked(itemInfo: ItemInfo, clickType: ClickType): boolean {
    if (!itemInfo) {
      return false;
    }
    let type: ItemType | undefined = itemInfo.itemType;
    let uri: string | undefined = itemInfo.uri;
    if (type === ItemType.CAMERA) { // 点击相机item
      console.info('onCameraClick');
      if (this.isBlock) {
        return false;
      }
      // 返回true则拉起系统相机，若应用需要自行处理则返回false。
      return true;
    } else if (type === ItemType.THUMBNAIL) { //点击图片、视频item（缩略图item）
      if (clickType === ClickType.SELECTED) { //选中
        if (this.isBlock) {
          return false;
        }
        // 将uri保存起来，并保证切换不同的宫格页组件时保持选中项一致
        if (uri) {
          this.selectedUris.push(uri)
          this.pickerOptions.preselectedUris = [...this.selectedUris];
        }
      } else { //反选
        if (uri) {
          // 排除反勾选uri，并保证切换不同的宫格页组件时保持选中项一致
          this.selectedUris = this.selectedUris.filter((item: string) => {
            return item !== uri;
          })
          this.pickerOptions.preselectedUris = [...this.selectedUris];
        }
      }
    } else {
    }
    console.info('onItemClicked' + JSON.stringify(itemInfo));
    return true;
  }
}