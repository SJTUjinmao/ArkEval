import { HashMap } from '@kit.ArkTS';
import { Logger } from '@ohos/base';
import { SortedHashMap } from '@ohos/base';
import { Chat } from '../../entities/Chat';
import { ArkTSUtils } from '@kit.ArkTS';

export class ChatDataSource implements IDataSource {
  private pinnedChatsMap: HashMap<string, Chat> = new HashMap<string, Chat>();
  private pinnedChatsArray: Chat[] =
    []; // instead using pinnedChatsMap.values() to iterate, we use this array to make it faster
  private sortedUnpinnedChatsMap: SortedHashMap<string, Chat> = new SortedHashMap<string, Chat>();
  // private readonly fetchInProgress: HashSet<number> = new HashSet<number>();
  private listeners: DataChangeListener[] = [];
  private lock_: ArkTSUtils.locks.AsyncLock = new ArkTSUtils.locks.AsyncLock(); // TODO: should we use a lock here?

  constructor() {
  }

  totalCount(): number {
    return this.sortedUnpinnedChatsMap.length() + this.pinnedChatsMap.length;
  }

  getData(index: number): Chat | undefined {
    if (index < this.pinnedChatsMap.length) {
      return this.pinnedChatsArray[index];
    }
    return this.sortedUnpinnedChatsMap.getValueAt(index - this.pinnedChatsMap.length);
  }

  get(chatId: string): Chat | undefined {
    // always check pinned chats first
    if (this.pinnedChatsMap.hasKey(chatId)) {
      return this.pinnedChatsMap.get(chatId);
    }
    return this.sortedUnpinnedChatsMap.get(chatId);
  }

  // ArkTS doesn't support generator so we cannot merge the iterators into one

  pinnedChats(): IterableIterator<Chat> {
    return this.pinnedChatsMap.values();
  }

  unsortedUnpinnedChats(): IterableIterator<Chat> {
    return this.sortedUnpinnedChatsMap.valuesIterator();
  }

  sortedUnpinnedChats(): IterableIterator<Chat> {
    return this.sortedUnpinnedChatsMap.sortedValuesIterator();
  }

  add(chat: Chat): void {
    Logger.debug('ChatDataSource add try to acquire lock');
    this.lock_.lockAsync(() => {
      Logger.debug('ChatDataSource add lock acquired');
      this._add(chat);
    }, ArkTSUtils.locks.AsyncLockMode.EXCLUSIVE);
  }

  _add(chat: Chat): void {
    // We should check the following cases in sequence:
    // 1. chat is already in pinned chats
    //  For case 1, we should modify the chat, but keep the chat in the same position.
    // 2. chat is already in unpinned chats
    //  For case 2, we should modify the chat, and if the lastMessageTimestamp is changed,
    //  we should move the chat to the correct position.
    // 3. chat is not in either pinned or unpinned chats
    //  For case 3, we should add the chat to the top of unpinned chats.
    // TODO: For case 2 and 3, do we need to move other chats down, or notifyReload()? Would that be too expensive?

    // case 1: chat is already in pinned chats
    if (this.pinnedChatsMap.hasKey(chat.chatId.toString())) {
      Logger.debug('ChatDataSource add chat is already in pinned chats, chat name: ' + chat.name);
      this.modify(chat);
      return;
    }

    // case 2: chat is already in unpinned chats
    let old_index = this.sortedUnpinnedChatsMap.indexOf(chat.chatId.toString());
    if (old_index >= 0) { // chat is already in unpinned chats
      Logger.debug('ChatDataSource add chat is already in unpinned chats, chat name: ' + chat.name + ', index: ' + old_index);
      let new_index = this.sortedUnpinnedChatsMap.set(chat.chatId.toString(), chat);
      if (new_index !== old_index) {
        Logger.debug('ChatDataSource add chat is already in unpinned chats, new_index !== old_index: ' + new_index + ' ' + old_index);
        // this.notifyDataMove(old_index + this.pinnedChatsMap.length, new_index + this.pinnedChatsMap.length);
        // this.notifyDataChange(new_index + this.pinnedChatsMap.length);
        this.notifyDataReload();
      } else {
        Logger.debug('ChatDataSource add chat is already in unpinned chats, new_index === old_index: ' + old_index);
        this.notifyDataChange(new_index + this.pinnedChatsMap.length);
      }
      return;
    } else {
      Logger.debug('ChatDataSource add new chat chat is not in any chats, old_index < 0, chat name:' + chat.name);
      // case 3: chat is not in either pinned or unpinned chats
      let new_index = -1;
      if (chat.pinned) {
        Logger.debug('ChatDataSource add new chat chat is not in any chats, chat is pinned, chat name:' + chat.name);
        this.pinnedChatsArray.push(chat);
        this.pinnedChatsMap.set(chat.chatId.toString(), chat);
        new_index = this.pinnedChatsArray.length - 1;
      } else {
        Logger.debug('ChatDataSource add new chat chat is not in any chats, chat is not pinned, chat name:' + chat.name);
        new_index = this.sortedUnpinnedChatsMap.set(chat.chatId.toString(), chat) + this.pinnedChatsMap.length;
      }
      this.notifyDataAdd(new_index); // TODO: test this
      return;
    }
  }

  addChatsFromPinnedAndUnpinnedArrays(pinnedChats: Chat[], unpinnedChats: Chat[]): void {
    this.pinnedChatsArray = pinnedChats;
    for (let i = 0; i < pinnedChats.length; i++) {
      this.pinnedChatsMap.set(pinnedChats[i].chatId.toString(), pinnedChats[i]);
    }
    for (let i = 0; i < unpinnedChats.length; i++) {
      this.sortedUnpinnedChatsMap.set(unpinnedChats[i].chatId.toString(), unpinnedChats[i]);
    }
    this.notifyDataReload();
  }

  async addChatsFromSingleArray(chats: Chat[]): Promise<void> {
    let start = Date.now();
    for (let i = 0; i < chats.length; i++) {
      if (chats[i].pinned) {
        this.pinnedChatsArray.push(chats[i]);
        this.pinnedChatsMap.set(chats[i].chatId.toString(), chats[i]);
      } else {
        this.sortedUnpinnedChatsMap.set(chats[i].chatId.toString(), chats[i]);
      }
    }
    this.notifyDataReload();
    let end = Date.now();
    Logger.debug('addChatsFromSingleArray elapsed: ' + (end - start) + ' ms');
  }

  addAllPinned(chats: Chat[]): void {
    this.pinnedChatsArray.push(...chats);
    let oldLength = this.pinnedChatsMap.length;
    for (let i = 0; i < chats.length; i++) {
      this.pinnedChatsMap.set(chats[i].chatId.toString(),
        chats[i]); // we cannot use setAll because it takes a HashMap but not an array
      this.notifyDataAdd(oldLength + i);
    }
    // this.notifyDataReload(); // no need to notify data reload since we assume number of pinned chats is limited
  }

  addAllUnpinned(chats: Chat[]): void {
    for (let i = 0; i < chats.length; i++) {
      this.sortedUnpinnedChatsMap.set(chats[i].chatId.toString(), chats[i]);
    }
    this.notifyDataReload();
  }

  delete(chatId: string): void {
    if (this.pinnedChatsMap.hasKey(chatId)) {
      this.pinnedChatsMap.remove(chatId);
      const index = this.pinnedChatsArray.findIndex(chat => chat.chatId.toString() === chatId);
      if (index >= 0) {
        this.pinnedChatsArray.splice(index, 1);
        this.notifyDataDelete(index);
      }
    } else {
      const index = this.sortedUnpinnedChatsMap.delete(chatId);
      if (index >= 0) {
        this.notifyDataDelete(index + this.pinnedChatsMap.length);
      }
    }
  }

  async deleteAt(index: number): Promise<void> {
    const chatId = (await this.getData(index))?.chatId.toString();
    if (!chatId) {
      return;
    }
    this.delete(chatId);
    this.notifyDataChange(index);
  }

  deleteAll(): void {
    this.pinnedChatsMap.clear();
    this.pinnedChatsArray = [];
    this.sortedUnpinnedChatsMap.clear();
    this.notifyDataReload();
  }

  modify(chat: Chat): void {
    let index = -1;
    if (chat.pinned) {
      const oldChat = this.pinnedChatsMap.get(chat.chatId.toString());
      if (oldChat) {
        index = this.pinnedChatsArray.indexOf(oldChat);
        this.pinnedChatsArray[index] = chat;
        this.pinnedChatsMap.set(chat.chatId.toString(), chat);
      } else {
        this.pinnedChatsArray.push(chat);
        this.pinnedChatsMap.set(chat.chatId.toString(), chat);
        index = this.pinnedChatsArray.length - 1;
      }
    } else {
      index = this.sortedUnpinnedChatsMap.set(chat.chatId.toString(), chat) + this.pinnedChatsMap.length;
    }
    Logger.debug('modify: ' + index);
    this.notifyDataChange(index);
  }

  // I don't think this method will be used but just in case we need it
  modifyAt(index: number, chat: Chat): void {
    this.sortedUnpinnedChatsMap.set(chat.chatId.toString(), chat);
    this.notifyDataChange(index);
  }

  registerDataChangeListener(listener: DataChangeListener): void {
    if (this.listeners.indexOf(listener) < 0) {
      console.info('add listener');
      this.listeners.push(listener);
    }
  }

  unregisterDataChangeListener(listener: DataChangeListener): void {
    const pos = this.listeners.indexOf(listener);
    if (pos >= 0) {
      console.info('remove listener');
      this.listeners.splice(pos, 1);
    }
  }

  // 通知LazyForEach组件需要重载所有子组件
  notifyDataReload(): void {
    Logger.debug('notifyDataReload');
    this.listeners.forEach(listener => {
      listener.onDataReloaded();
    })
  }

  // 通知LazyForEach组件需要在index对应索引处添加子组件
  notifyDataAdd(index: number): void {
    Logger.debug('notifyDataAdd: ' + index);
    this.listeners.forEach(listener => {
      listener.onDataAdd(index);
    })
  }

  // 通知LazyForEach组件在index对应索引处数据有变化，需要重建该子组件
  notifyDataChange(index: number): void {
    Logger.debug('notifyDataChange: ' + index);
    this.listeners.forEach(listener => {
      listener.onDataChange(index);
    })
  }

  // 通知LazyForEach组件需要在index对应索引处删除该子组件
  notifyDataDelete(index: number): void {
    Logger.debug('notifyDataDelete: ' + index);
    this.listeners.forEach(listener => {
      listener.onDataDelete(index);
    })
  }

  // 通知LazyForEach组件将from索引和to索引处的子组件进行交换
  notifyDataMove(from: number, to: number): void {
    Logger.debug('notifyDataMove: ' + from + ' ' + to);
    this.listeners.forEach(listener => {
      listener.onDataMove(from, to);
    })
  }
}