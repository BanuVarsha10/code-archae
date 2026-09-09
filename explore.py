from pydriller import Repository

count = 0
for commit in Repository('httpx').traverse_commits():
    print(f'{commit.hash[:8]}  {commit.author_date.date()}  {commit.author.name:20.20}  {commit.msg.splitlines()[0][:50]}')
    count += 1
    if count >= 8:
        break
