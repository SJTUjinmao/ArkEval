import sql from "@liushengyi/smartdb"
import { SeenChat } from "../entities/SeenChat"
import { DatabaseConstants } from "../constants/DatabaseConstants"

class SeenChatDao {
  constructor() {
  }

  @sql.Sql(`CREATE TABLE IF NOT EXISTS ${DatabaseConstants.SEEN_CHATS_TABLE_NAME} (
    chatId INTEGER NOT NULL,
    chatType INTEGER NOT NULL,
    packedChat TEXT NOT NULL,
    isContact BOOLEAN NOT NULL,
    isMutualContact BOOLEAN NOT NULL,
    phone TEXT,
    username TEXT,
    photoThumb TEXT,
    fullName TEXT NOT NULL,
    firstName TEXT NOT NULL,
    lastName TEXT,
    bio TEXT,
    dateOfBirth INTEGER,
    forum BOOLEAN DEFAULT FALSE,
  	PRIMARY KEY (chatId)
)`)
  createSeenChatsTable(): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.Sql(`DROP TABLE IF EXISTS ${DatabaseConstants.SEEN_CHATS_TABLE_NAME}`)
  deleteSeenChatsTable(): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO seen_chats (chatId, chatType, packedChat, isContact, isMutualContact, phone, username, usernames, " +
    "photoThumb, fullName, firstName, lastName, bio, dateOfBirth, forum) " +
    "VALUES (#{chatId}, #{chatType}, #{packedChat}, #{isContact}, #{isMutualContact}, #{phone}, #{username}, #{photoThumb}, #{fullName}, " +
    "#{firstName}, #{lastName}, #{bio}, #{dateOfBirth}, #{forum})")
  insert(@sql.Param("chatId") chatId: number, @sql.Param("chatType") chatType: number,
    @sql.Param("isContact") isContact: boolean,
    @sql.Param("isMutualContact") isMutualContact: boolean, @sql.Param("phone") phone: string,
    @sql.Param("username") username: string,
    @sql.Param("photoThumb") photoThumb: string, @sql.Param("fullName") fullName: string,
    @sql.Param("firstName") firstName: string, @sql.Param("lastName") lastName: string, @sql.Param("bio") bio: string,
    @sql.Param("dateOfBirth") dateOfBirth: number, @sql.Param("forum") forum: boolean): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlInsert("REPLACE INTO seen_chats (chatId, chatType, packedChat, isContact, isMutualContact, phone, username, " +
    "photoThumb, fullName, firstName, lastName, bio, dateOfBirth, forum) " +
    "VALUES (#{seenChat.chatId}, #{seenChat.chatType}, #{seenChat.packedChat}, #{seenChat.isContact}, " +
    "#{seenChat.isMutualContact}, #{seenChat.phone}, #{seenChat.username}, #{seenChat.photoThumb}, " +
    "#{seenChat.fullName}, #{seenChat.firstName}, #{seenChat.lastName}, #{seenChat.bio}, #{seenChat.dateOfBirth}, #{seenChat.forum})")
  insertSeenChat(@sql.Param("seenChat") seenChat: SeenChat): Promise<void> {
    return sql.PromiseNull();
  }

  @sql.SqlQuery("SELECT * FROM seen_chats")
  @sql.ReturnType([SeenChat])
  getAllSeenChats(): Promise<SeenChat[]> {
    return sql.PromiseNull();
  }

  @sql.SqlQuery("SELECT * FROM seen_chats WHERE chatId = #{chatId}")
  @sql.ReturnType(SeenChat)
  getSeenChatById(@sql.Param("chatId") chatId: number): Promise<SeenChat> {
    return sql.PromiseNull();
  }
}

export const seenChatDao = new SeenChatDao();