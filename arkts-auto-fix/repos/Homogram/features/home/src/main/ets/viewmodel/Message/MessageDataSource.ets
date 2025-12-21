import { SortedHashMap } from '@ohos/base';
import { Message } from '../../entities/Message';
import { IDataSourcePrefetching } from '@kit.ArkUI';

const nullMessage = new Message(0, 0, false, false, 0, '', 0, '', 0);

// Caching logic:
// 1. Always caching the latest 100 messages.
// 2. When the message list is updated, the new messages will be added to the cache,
// and the oldest messages will be removed.
// 3. When user scrolls
export class MessageDataSource implements IDataSourcePrefetching {
  public sortedMessages: SortedHashMap<number, Message> = new SortedHashMap<number, Message>();
  private listeners: DataChangeListener[] = [];

  public setMessages(messages: Message[]): void {
    for (let i = 0; i < messages.length; i++) {
      this.sortedMessages.set(messages[i].messageId, messages[i]);
    }
    this.notifyDataReload();
  }

  prefetch(index: number): void | Promise<void> {
    throw new Error('Method not implemented.');
  }

  cancel?(index: number): void | Promise<void> {
    throw new Error('Method not implemented.');
  }

  public totalCount(): number {
    return this.sortedMessages.length();
  }

  // to satisfy the interface
  public getData(index: number): Message {
    return this.sortedMessages.getValueAt(index) || nullMessage;
  }

  // to make names consistent
  public get(index: number): Message {
    return this.sortedMessages.getValueAt(index) || nullMessage;
  }

  public getMessageById(messageId: number): Message | undefined {
    return this.sortedMessages.get(messageId);
  }

  public getAll(): Message[] {
    return this.sortedMessages.getSortedValues();
  }

  public indexOf(messageId: number): number {
    return this.sortedMessages.indexOf(messageId);
  }

  public push(data: Message): void {
    this.notifyDataAdd(this.sortedMessages.set(data.messageId, data));
  }

  public append(data: Message[]): void {
    for (let i = 0; i < data.length; i++) {
      this.notifyDataAdd(this.sortedMessages.set(data[i].messageId, data[i]));
    }
  }

  public delete(index: number): void {
    let value = this.sortedMessages.getValueAt(index);
    if (value) {
      this.sortedMessages.delete(value.messageId);
      this.notifyDataDelete(index);
    }

  }

  //
  // public move(from: number, to: number): void {
  //   // TODO: optimize
  //   let temp: Message = this.sortedMessages[from];
  //   this.sortedMessages[from] = this.sortedMessages[to];
  //   this.sortedMessages[to] = temp;
  //   this.notifyDataMove(from, to);
  // }

  public reload(): void {
    this.notifyDataReload();
  }

  public modify(index: number, data: Message): void {
    let id = this.sortedMessages.getValueAt(index)?.messageId;
    if (id) {
      this.sortedMessages.set(id, data);
      this.notifyDataChange(index);
    }
  }

  registerDataChangeListener(listener: DataChangeListener): void {
    if (this.listeners.indexOf(listener) < 0) {
      console.info('add listener');
      this.listeners.push(listener);
    }
  }

  // 通知LazyForEach组件需要重载所有子组件
  notifyDataReload(): void {
    this.listeners.forEach(listener => {
      listener.onDataReloaded();
    })
  }

  // 通知LazyForEach组件需要在index对应索引处添加子组件
  notifyDataAdd(index: number): void {
    this.listeners.forEach(listener => {
      listener.onDataAdd(index);
    })
  }

  // 通知LazyForEach组件在index对应索引处数据有变化，需要重建该子组件
  notifyDataChange(index: number): void {
    this.listeners.forEach(listener => {
      listener.onDataChange(index);
    })
  }

  // 通知LazyForEach组件需要在index对应索引处删除该子组件
  notifyDataDelete(index: number): void {
    this.listeners.forEach(listener => {
      listener.onDataDelete(index);
    })
  }

  // 通知LazyForEach组件将from索引和to索引处的子组件进行交换
  notifyDataMove(from: number, to: number): void {
    this.listeners.forEach(listener => {
      listener.onDataMove(from, to);
    })
  }

  unregisterDataChangeListener(listener: DataChangeListener): void {
    const pos = this.listeners.indexOf(listener);
    if (pos >= 0) {
      console.info('remove listener');
      this.listeners.splice(pos, 1);
    }
  }
}