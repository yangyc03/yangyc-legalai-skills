use framework "Foundation"
use scripting additions

on run {input, parameters}
	if (count of input) is 0 then
		display dialog "请先选择要移动的文件或文件夹。" buttons {"好"} default button "好" with icon caution
		return input
	end if

	set desktopPath to POSIX path of (path to desktop folder)
	set desktopRoot to my trimTrailingSlash(desktopPath)
	set movedCount to 0
	set notes to {}

	repeat with inputItem in input
		set sourceName to "未知项目"
		set replacementRemoved to false
		set sourcePath to missing value
		try
			set sourcePath to my trimTrailingSlash(POSIX path of inputItem)
			if my pathIsSymbolicLink(sourcePath) then error "为避免误移动链接指向的真实项目，本版本不移动符号链接。" number 1003
			if my pathExists(sourcePath) is false then error "来源项目已不存在。" number 1001

			set sourceName to do shell script "/usr/bin/basename " & quoted form of sourcePath
			set sourceParentPath to do shell script "/usr/bin/dirname " & quoted form of sourcePath

			if my pathsReferToSameItem(sourcePath, desktopRoot) then
				set end of notes to sourceName & "（桌面文件夹本身不能移动）"
			else if my pathsReferToSameItem(sourceParentPath, desktopRoot) then
				set end of notes to sourceName & "（已在桌面，未移动）"
			else
				set targetPath to desktopPath & sourceName
				if my pathExists(targetPath) then
					if my pathIsSymbolicLink(targetPath) then error "桌面同名项目是符号链接。为避免影响链接指向的真实项目，本版本不自动处理。" number 1004
					set choice to button returned of (display dialog "桌面已有“" & sourceName & "”。" buttons {"取消", "保留两者", "替换"} default button "保留两者" with icon caution)
					if choice is "取消" then
						set end of notes to sourceName & "（已取消）"
					else if choice is "替换" then
						my moveItemToTrash(targetPath)
						set replacementRemoved to true
						my moveItemExact(sourcePath, targetPath)
						set movedCount to movedCount + 1
					else
						set isDirectory to my pathIsDirectory(sourcePath)
						set nameParts to my splitName(sourceName, isDirectory)
						set rootName to item 1 of nameParts
						set extensionPart to item 2 of nameParts
						set itemIndex to 2
						set uniqueName to rootName & "_" & my formattedIndex(itemIndex) & extensionPart
						repeat while my pathExists(desktopPath & uniqueName)
							set itemIndex to itemIndex + 1
							set uniqueName to rootName & "_" & my formattedIndex(itemIndex) & extensionPart
						end repeat
						set uniquePath to desktopPath & uniqueName
						repeat
							if my pathExists(uniquePath) is false then
								try
									my moveItemExact(sourcePath, uniquePath)
									exit repeat
								on error moveMessage number moveNumber
									if my pathExists(uniquePath) is false then error moveMessage number moveNumber
								end try
							end if
							set itemIndex to itemIndex + 1
							set uniqueName to rootName & "_" & my formattedIndex(itemIndex) & extensionPart
							set uniquePath to desktopPath & uniqueName
						end repeat
						set movedCount to movedCount + 1
					end if
				else
					my moveItemExact(sourcePath, targetPath)
					set movedCount to movedCount + 1
				end if
			end if
		on error errMsg number errNum
			if errNum is not -128 then
				if replacementRemoved then
					if sourcePath is not missing value and my pathExists(sourcePath) then
						set end of notes to sourceName & "（来源仍在原处；桌面原同名项目已移入废纸篓，可恢复。原因：" & errMsg & "）"
					else
						set end of notes to sourceName & "（替换未完整完成；桌面原同名项目已移入废纸篓，请核对来源。原因：" & errMsg & "）"
					end if
				else
					set end of notes to sourceName & "（未能移动：" & errMsg & "）"
				end if
			end if
		end try
	end repeat

	set AppleScript's text item delimiters to return
	set notesText to notes as text
	set AppleScript's text item delimiters to ""
	set summaryText to "已移动 " & movedCount & " 个项目。"
	if notesText is not "" then set summaryText to summaryText & return & notesText
	display dialog summaryText buttons {"好"} default button "好"
	return input
end run

on trimTrailingSlash(thePath)
	if thePath ends with "/" and (length of thePath) > 1 then return text 1 thru -2 of thePath
	return thePath
end trimTrailingSlash

on pathExists(thePath)
	try
		do shell script "/bin/test -e " & quoted form of thePath
		return true
	on error
		return false
	end try
end pathExists

on pathIsDirectory(thePath)
	try
		do shell script "/bin/test -d " & quoted form of thePath
		return true
	on error
		return false
	end try
end pathIsDirectory

on pathIsSymbolicLink(thePath)
	try
		do shell script "/bin/test -L " & quoted form of thePath
		return true
	on error
		return false
	end try
end pathIsSymbolicLink

on moveItemExact(sourcePath, targetPath)
	set fileManager to current application's NSFileManager's defaultManager()
	set {didMove, moveError} to fileManager's moveItemAtPath:sourcePath toPath:targetPath |error|:(reference)
	if (didMove as boolean) is false then error (moveError's localizedDescription() as text) number 1002
end moveItemExact

on moveItemToTrash(targetPath)
	set fileManager to current application's NSFileManager's defaultManager()
	set targetURL to current application's NSURL's fileURLWithPath:targetPath
	set {didTrash, resultURL, trashError} to fileManager's trashItemAtURL:targetURL resultingItemURL:(reference) |error|:(reference)
	if (didTrash as boolean) is false then error (trashError's localizedDescription() as text) number 1005
end moveItemToTrash

on pathsReferToSameItem(firstPath, secondPath)
	try
		set firstIdentity to do shell script "/usr/bin/stat -f '%d:%i' -- " & quoted form of firstPath
		set secondIdentity to do shell script "/usr/bin/stat -f '%d:%i' -- " & quoted form of secondPath
		return firstIdentity is secondIdentity
	on error
		return false
	end try
end pathsReferToSameItem

on formattedIndex(itemIndex)
	if itemIndex < 10 then return "0" & (itemIndex as text)
	return itemIndex as text
end formattedIndex

on splitName(itemName, isDirectory)
	if isDirectory then return {itemName, ""}
	set oldDelimiters to AppleScript's text item delimiters
	set AppleScript's text item delimiters to "."
	set nameItems to text items of itemName
	set AppleScript's text item delimiters to oldDelimiters

	if (count of nameItems) is 1 then return {itemName, ""}
	if (count of nameItems) is 2 and item 1 of nameItems is "" then return {itemName, ""}
	if item -1 of nameItems is "" then return {itemName, ""}

	set extensionPart to "." & item -1 of nameItems
	set rootLength to (length of itemName) - (length of extensionPart)
	if rootLength < 1 then return {itemName, ""}
	return {text 1 thru rootLength of itemName, extensionPart}
end splitName
