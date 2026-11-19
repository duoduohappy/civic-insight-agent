"""Deterministic, entirely fictional 12345 demonstration data."""
import random
from datetime import datetime, timedelta

AS_OF = datetime(2026, 11, 19, 12)
DISTRICTS = ['东城区', '西城区', '南城区', '北城区', '滨江区', '高新区', '湖滨区', '新城区']
DEPTS = ['城市管理局', '住房建设局', '生态环境局', '交通运输局', '市场监管局', '人力资源局', '教育局', '水务局']
CATEGORIES = ['市容环境', '物业管理', '噪声污染', '道路交通', '消费维权', '劳动保障', '教育服务', '供水排水']
SUBJECTS = [
    ['道路堆物影响通行', '流动摊贩占道经营', '生活垃圾清运不及时', '路灯损坏影响出行'],
    ['小区公共设施维修', '物业收费标准争议', '电梯故障迟迟未修', '公共区域停车纠纷'],
    ['夜间施工噪声扰民', '餐饮油烟影响生活', '娱乐场所噪声投诉', '工业异味影响环境'],
    ['公交站点设施损坏', '路口交通标线模糊', '公交班次间隔过长', '道路坑洼影响通行'],
    ['商家售后退款争议', '预付卡退费困难', '商品质量投诉', '商户价格标识不清'],
    ['工资发放延迟', '劳动合同办理咨询', '社会保险缴纳争议', '劳动权益保障咨询'],
    ['学校周边通行问题', '入学政策咨询', '培训机构退费争议', '校园设施维护诉求'],
    ['供水压力不足', '雨水井盖破损', '道路积水排水不畅', '自来水水质反馈'],
]
USERS = {
    'city': {'name': '林悦', 'role': '市级管理者', 'scope': '全市工单', 'district': None, 'department_id': None, 'assignee': None},
    'district': {'name': '周宁', 'role': '区级管理者', 'scope': '东城区工单', 'district': '东城区', 'department_id': None, 'assignee': None},
    'department': {'name': '陈岚', 'role': '部门负责人', 'scope': '城市管理局工单', 'district': None, 'department_id': 1, 'assignee': None},
    'staff': {'name': '李明', 'role': '经办人员', 'scope': '分配给李明的工单', 'district': None, 'department_id': 1, 'assignee': '李明'},
}


def generate():
    r = random.Random(12345)
    tickets, events, feedback = [], [], []
    for n in range(1, 24001):
        cat = r.randrange(len(CATEGORIES))
        district = r.choice(DISTRICTS)
        created = AS_OF - timedelta(days=r.randrange(365), hours=r.randrange(24))
        if created.month == 11 and r.random() < .25:
            cat, district = 2, '东城区'
        subject = r.choice(SUBJECTS[cat])
        due = created + timedelta(days=[5, 10, 3, 7, 7, 10, 7, 3][cat])
        age = (AS_OF - created).total_seconds() / 86400
        done = r.random() < (0.94 if age > 15 else 0.46)
        hours = r.randint(8, [180, 340, 130, 210, 200, 300, 180, 110][cat])
        closed = min(created + timedelta(hours=hours), AS_OF) if done else None
        overdue = int((closed or AS_OF) > due)
        transfers = r.choices([0, 1, 2, 3], [70, 19, 8, 3])[0]
        status = '已办结' if done else ('待签收' if age < 2 and r.random() < .5 else '办理中')
        tid = f'GD2026{n:05}'
        text = f'市民反映{district}示范街区{subject}，希望相关部门核查并反馈。'
        if district == '东城区' and created.month == 11 and cat == 2:
            text += '附近施工项目近期集中开展夜间作业。'
        assignee = '李明' if cat == 0 and n % 3 == 0 else ['王芳', '赵磊', '刘敏'][n % 3]
        fmt = lambda d: d.strftime('%Y-%m-%d %H:%M') if d else None
        tickets.append((tid, subject, text, district, CATEGORIES[cat], cat+1, assignee,
                        r.choice(['电话', '网站', '微信', '移动端']), status, fmt(created), fmt(due), fmt(closed),
                        overdue, transfers, round((closed-created).total_seconds()/3600, 1) if closed else None))
        stages = [('受理', created, '12345中心', '诉求已登记'), ('派单', created+timedelta(minutes=20), DEPTS[cat], '按模拟职责分派')]
        for k in range(transfers):
            stages.append(('转派', created+timedelta(hours=k+1), DEPTS[cat], '补充核实承办职责'))
        if done:
            stages.append(('办结', closed, DEPTS[cat], '已核查并反馈办理情况'))
        for stage, at, actor, note in sorted(stages, key=lambda x: x[1]):
            events.append((len(events)+1, tid, stage, fmt(at), actor, note))
        if done and r.random() < .83:
            score = r.choices([1, 2, 3, 4, 5], [3+overdue*8, 3, 9, 25, 60])[0]
            feedback.append((len(feedback)+1, tid, score, '满意' if score >= 4 else '不满意' if score <= 2 else '一般',
                             '处理及时，结果清楚' if score >= 4 else '希望进一步跟进处理', fmt(min(closed+timedelta(hours=2), AS_OF))))
    docs = []
    for i, cat in enumerate(CATEGORIES):
        docs.append((f'KB{i+1:03}', cat+'受理与派单指引', '职责指引', None, i+1,
                     f'【模拟规则】{cat}诉求由{DEPTS[i]}牵头核实。跨部门事项须记录转派理由。存在职责争议时提交协调，不得无理由反复退单。', '2026-01-01'))
        docs.append((f'KB{i+21:03}', cat+'回访质量规范', '回访规范', None, i+1,
                     f'【模拟规则】{cat}工单办结后进行回访，评分4至5为满意、3为一般、1至2为不满意。未回访工单不进入满意率分母。办结不等同于市民满意。', '2026-01-01'))
    docs.append(('KB100', '东城区夜间施工专项协调纪要', '协调纪要', '东城区', 3,
                 '【模拟纪要】11月示范街区多个项目集中夜间施工。建议联合核实作业时间并建立跨部门联系机制。本纪要仅为背景线索，不能单独证明投诉变化的因果关系。', '2026-11-05'))
    context = []
    for days in range(365):
        day = AS_OF - timedelta(days=days)
        for district in DISTRICTS:
            rain = round(r.random()*50, 1) if day.month in [6,7,8] else round(r.random()*8, 1)
            context.append((day.strftime('%Y-%m-%d'), district, rain, int(day.weekday() >= 5)))
    return {'tickets': tickets, 'events': events, 'feedback': feedback,
            'departments': [(i+1, name, CATEGORIES[i], '模拟职责：'+CATEGORIES[i]+'相关诉求') for i, name in enumerate(DEPTS)],
            'service_targets': [(i+1, CATEGORIES[i], [5,10,3,7,7,10,7,3][i], 95.0, 90.0) for i in range(len(DEPTS))],
            'knowledge_docs': docs, 'daily_context': context,
            'district_profiles': [(d, 30+i*7, 8+i*2, 200+i*33) for i,d in enumerate(DISTRICTS)]}


DATA = generate()
